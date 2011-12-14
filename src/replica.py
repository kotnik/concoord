'''
@author: denizalti
@note: The Replica keeps an object and responds to Perform messages received from the Leader.
@date: February 1, 2011
'''
from threading import Thread, Lock, Condition, Timer, Event
import operator
import time
import random
import math
import sys
import os
import signal

from pprint import pprint
from node import *
from enums import *
from utils import *
from connection import Connection, ConnectionPool
from group import Group
from peer import Peer
from message import *
from command import Command
from pvalue import PValue, PValueSet
from exception import *

backoff_event = Event()
# Class used to collect responses to both PREPARE and PROPOSE messages
class ResponseCollector():
    """ResponseCollector keeps the state related to both MSG_PREPARE and
    MSG_PROPOSE.
    """
    def __init__(self, acceptors, ballotnumber, commandnumber, proposal):
        """ResponseCollector State
        - ballotnumber: ballotnumber for the corresponding MSG
        - commandnumber: commandnumber for the corresponding MSG
        - proposal: proposal for the corresponding MSG
        - acceptors: Group of Acceptor nodes for the corresponding MSG
        - received: dictionary that keeps <peer:reply> mappings
        - ntotal: # Acceptor nodes for the corresponding MSG
        - nquorum: # ACCEPTs needed for success
        - possiblepvalueset: Set of pvalues collected from Acceptors
        """
        self.ballotnumber = ballotnumber
        self.commandnumber = commandnumber
        self.proposal = proposal
        self.acceptors = acceptors
        self.sent = [] # msgids
        self.received = {} # peer:msg
        self.ntotal = len(self.acceptors)
        self.nquorum = self.ntotal/2+1
        self.possiblepvalueset = PValueSet()

class Replica(Node):
    """Replica receives MSG_PERFORM from Leaders and execute corresponding commands."""
    def __init__(self, nodetype=NODE_REPLICA, instantiateobj=True, port=None,  bootstrap=None):
        """Replica State
        - object: the object that Replica is replicating
	- nexttoexecute: the commandnumber that relica is waiting for to execute
        - decisions: received requests as <commandnumber:command> mappings
        - executed: commands that are executed as <command:commandresult> mappings
        - proposals: proposals that are made by this replica kept as <commandnumber:command> mappings
        - pendingcommands: Set of unissiued commands that are waiting for the window to roll over to be issued
        """
        Node.__init__(self, nodetype, instantiateobj=instantiateobj)
        if instantiateobj:
            try:
                self.object = getattr(__import__(self.objectfilename[:-3], globals(), locals(), [], -1), self.objectname)()
                infofile = open(self.objectfilename[:-3]+"-descriptor", 'w')
                infofile.write("%s:%d" %(self.addr,self.port))
                infofile.close()
            except Exception as e:
                self.logger.write("Object Error", "Object cannot be found.")
        self.leader_initializing = False
        self.nexttoexecute = 1
        # decided commands: <commandnumber:command>
        self.decisions = {}
        self.decidedcommandset = set()
        # executed commands: <command:(replycode,commandresult,unblocked{})>
        self.executed = {}
        # commands that are proposed: <commandnumber:command>
        self.proposals = {}
        # commands that are received, not yet proposed: <commandnumber:command>
        self.pendingcommands = {}
        # commandnumbers known to be in use
        self.usedcommandnumbers = set()
        # number for metacommands initiated from this replica
        self.metacommandnumber = 0
        # inconsistent client reads
        self.callsfromclient = 0
        self.clientpool = ConnectionPool()

    def startservice(self):
        """Start the background services associated with a replica."""
        Node.startservice(self)
        leaderping_thread = Timer(LIVENESSTIMEOUT, self.ping_leader)
        leaderping_thread.start()

    def performcore(self, msg, slotnumber, dometaonly=False, designated=False):
        """The core function that performs a given command in a slot number. It 
        executes regular commands as well as META-level commands (commands related
        to the managements of the Paxos protocol) with a delay of WINDOW commands."""
        self.logger.write("State:", "---> SlotNumber: %d Command: %s DoMetaOnly: %s" % (slotnumber, self.decisions[slotnumber], dometaonly))
        command = self.decisions[slotnumber]
        commandlist = command.command.split()
        commandname = commandlist[0]
        commandargs = commandlist[1:]
        ismeta = (commandname in METACOMMANDS)
        noop = (commandname == "noop")
        send_result_to_client = True
        # Result triple
        clientreplycode, givenresult, unblocked = (-1, None, {})
        try:
            if dometaonly and not ismeta:
                return
            elif noop:
                method = getattr(self, NOOP)
                clientreplycode = CR_OK
                givenresult = "NOOP"
                unblocked = {}
                send_result_to_client = False
            elif dometaonly and ismeta:
                # execute a metacommand when the window has expired
                method = getattr(self, commandname)
                clientreplycode = CR_META
                givenresult = method(*commandargs)
                unblocked = {}
                send_result_to_client = False
            elif not dometaonly and ismeta:
                # meta command, but the window has not passed yet, 
                # so just mark it as executed without actually executing it
                # the real execution will take place when the window has expired
                self.add_to_executed(self.decisions[slotnumber], (CR_META, META, {}))
                return
            elif not dometaonly and not ismeta:
                # this is the workhorse case that executes most normal commands
                method = getattr(self.object, commandname)
                # Watch out for the lock release and acquire!
                self.lock.release()
                try:
                    givenresult = method(*commandargs, _concoord_command=command)
                    clientreplycode = CR_OK
                    send_result_to_client = True
                except BlockingReturn as blockingretexp:
                    givenresult = blockingretexp.returnvalue
                    clientreplycode = CR_BLOCK
                    send_result_to_client = True
                except UnblockingReturn as unblockingretexp:
                    # Get the information about the method call
                    # These will be used to update executed and
                    # to send reply message to the caller client
                    givenresult = unblockingretexp.returnvalue
                    unblocked = unblockingretexp.unblocked
                    clientreplycode = CR_OK
                    send_result_to_client = True
                    # If there are clients to be unblocked that have
                    # been blocked previously send them unblock messages
                    for unblockedclientcommand in unblocked.iterkeys():
                        self.send_reply_to_client(CR_UNBLOCK, None, unblockedclientcommand)
                except Exception as e:
                    self.logger.write("Execution Error", "Error during method invocation: %s" % e)
                    givenresult = e
                    clientreplycode = CR_EXCEPTION
                    send_result_to_client = True
                    unblocked = {}
                self.lock.acquire()
        except (TypeError, AttributeError) as t:
            self.logger.write("Execution Error", "command not supported: %s" % (command))
            givenresult = 'COMMAND NOT SUPPORTED'
            clientreplycode = CR_EXCEPTION
            unblocked = {}
            send_result_to_client = True
        self.add_to_executed(self.decisions[slotnumber], (clientreplycode,givenresult,unblocked))
        
        if commandname not in METACOMMANDS:
            # if this client contacted me for this operation, return him the response 
            if send_result_to_client and self.type == NODE_LEADER and command.client.getid() in self.clientpool.poolbypeer.keys():
                #endtimer(command, 1)
                self.send_reply_to_client(clientreplycode, givenresult, command)

        if slotnumber % GARBAGEPERIOD == 0 and self.type == NODE_LEADER:
            garbagecommand = Command(self.me, self.metacommandnumber, "garbage_collect %d" % slotnumber)
            self.metacommandnumber += 1
            if self.leader_initializing:
                self.handle_client_command(garbagecommand, prepare=True)
            else:
                self.handle_client_command(garbagecommand)

    def send_reply_to_client(self, clientreplycode, givenresult, command):
        self.logger.write("State", "Sending REPLY to CLIENT")
        clientreply = ClientReplyMessage(MSG_CLIENTREPLY, self.me, reply=givenresult, replycode=clientreplycode, inresponseto=command.clientcommandnumber)
        self.logger.write("State", "Clientreply: %s\nAcceptors: %s" % (str(clientreply), str(self.groups[NODE_ACCEPTOR])))
        clientconn = self.clientpool.get_connection_by_peer(command.client)
        if clientconn.thesocket == None:
            self.logger.write("State", "Client disconnected.")
            return
        clientconn.send(clientreply)

    def perform(self, msg, designated=False):
        """Take a given PERFORM message, add it to the set of decided commands, and call performcore to execute."""
        if msg.commandnumber not in self.decisions:
            self.add_to_decisions(msg.commandnumber, msg.proposal)
        # If replica was using this commandnumber for a different proposal, initiate it again
        if self.proposals.has_key(msg.commandnumber) and msg.proposal != self.proposals[msg.commandnumber]:
            self.do_command_propose(self.proposals[msg.commandnumber])
            
        while self.decisions.has_key(self.nexttoexecute):
            requestedcommand = self.decisions[self.nexttoexecute]
            if requestedcommand in self.executed:
                self.logger.write("State", "previously executed command %d." % self.nexttoexecute)
                # If we are a leader, we should send a reply to the client for this command
                # in case the client didn't receive the reply from the previous leader
                if self.type == NODE_LEADER:
                    prevrcode, prevresult, prevunblocked = self.executed[requestedcommand]
                    if prevrcode == CR_BLOCK:
                        # XXX: As dictionary is not sorted we have to start from the beginning every time
                        for resultset in self.executed.itervalues():
                            if resultset[UNBLOCKED] == requestedcommand:
                                # This client has been UNBLOCKED
                                prevresult = None
                                prevrcode = CR_UNBLOCK
                    self.logger.write("State", "Sending reply to client.")
                    self.send_reply_to_client(prevrcode, prevresult, requestedcommand)
                self.nexttoexecute += 1
                # the window just got bumped by one
                # check if there are pending commands, and issue one of them
                self.issue_pending_command(self.nexttoexecute)
            elif requestedcommand not in self.executed:
                self.logger.write("State", "executing command %d." % self.nexttoexecute)
                # check to see if there was a meta command precisely WINDOW commands ago that should now take effect
                # We are calling performcore 2 times, the timing gets screwed plus this is very unefficient :(
                if self.nexttoexecute > WINDOW:
                    self.performcore(msg, self.nexttoexecute - WINDOW, True, designated=designated)
                self.performcore(msg, self.nexttoexecute, designated=designated)
                self.nexttoexecute += 1
                # the window just got bumped by one
                # check if there are pending commands, and issue one of them
                self.issue_pending_command(self.nexttoexecute)
            
    def issue_pending_command(self, candidatecommandno):
        """propose a command from the pending commands"""
        if self.pendingcommands.has_key(candidatecommandno):
            self.do_command_propose_frompending(candidatecommandno)

    def msg_perform(self, conn, msg):
        """received a PERFORM message, perform it and send an UPDATE message to the source if necessary"""
        self.perform(msg)

        if not self.stateuptodate and (self.type == NODE_REPLICA or self.type == NODE_NAMESERVER):
            self.logger.write("State", "Updating..")
            if msg.commandnumber == 1:
                self.stateuptodate = True
                return
            updatemessage = UpdateMessage(MSG_UPDATE, self.me)
            self.send(updatemessage, peer=msg.source)
            
    def msg_update(self, conn, msg):
        """someone needs to be updated on the set of past decisions, send our decisions"""
        updatereplymessage = UpdateMessage(MSG_UPDATEREPLY, self.me, self.decisions)
        self.send(updatereplymessage, peer=msg.source)

    def msg_updatereply(self, conn, msg):
        """merge decisions received with local decisions"""
        for key,value in self.decisions.iteritems():
            if msg.decisions.has_key(key):
                assert self.decisions[key] == msg.decisions[key], "Update Error"
        # update decisions cumulatively
        self.decisions.update(msg.decisions)
        self.decidedcommandset = set(self.decisions.values())
        self.usedcommandnumbers = self.usedcommandnumbers.union(set(self.decisions.keys()))
        self.stateuptodate = True

    def do_noop(self):
        pass

    def _add_node(self, nodetype, nodename):
        self.logger.write("State", "Adding node: %s %s" % (nodetype, nodename))
        ipaddr,port = nodename.split(":")
        nodetype = int(nodetype)
        nodepeer = Peer(ipaddr,int(port),nodetype)
        self.groups[nodetype].add(nodepeer)
        
    def _del_node(self, nodetype, nodename):
        self.logger.write("State", "Deleting node: %s %s" % (nodetype, nodename))
        ipaddr,port = nodename.split(":")
        nodetype = int(nodetype)
        nodepeer = Peer(ipaddr,int(port),nodetype)
        self.groups[nodetype].remove(nodepeer)

    def _garbage_collect(self, garbagecommandnumber):
        """ garbage collect """
        self.logger.write("State", "Initiating garbage collection upto cmd#%d" % garbagecommandnumber)
        garbagemsg = GarbageCollectMessage(MSG_GARBAGECOLLECT,self.me,commandnumber=garbagecommandnumber,snapshot=self.object)
        self.send(garbagemsg,group=self.groups[NODE_ACCEPTOR])

    def cmd_showobject(self, args):
        """shell command [showobject]: print replicated object information""" 
        print self.object

    def cmd_info(self, args):
        """shell command [info]: print next commandnumber to execute and executed commands"""
        print "Waiting to execute #%d" % self.nexttoexecute
        print "Decisions:\n"
        for (commandnumber,command) in self.decisions.iteritems():
            temp = "%d:\t%s" %  (commandnumber, command)
            if command in self.executed:
                temp += "\t%s\n" % (str(self.executed[command]))
            print temp
            
# LEADER STATE
    def become_leader(self):
        """Leader State
        - active: indicates if the Leader has a *good* ballotnumber
        - ballotnumber: the highest ballotnumber Leader has used
        - outstandingprepares: ResponseCollector dictionary for MSG_PREPARE,
        indexed by ballotnumber
        - outstandingproposes: ResponseCollector dictionary for MSG_PROPOSE,
        indexed by commandnumber
        - receivedclientrequests: commands received from clients as
        <(client,clientcommandnumber):command> mappings
        - clientpool: connections to clients
        - backoff: backoff amount that is used to determine how much a leader should
        backoff during a collusion
        """
        if self.type != NODE_LEADER:
            self.type = NODE_LEADER
            self.active = False
            self.ballotnumber = (0,self.id)
            self.outstandingprepares = {}
            self.outstandingproposes = {}
            self.receivedclientrequests = {} # indexed by (client,clientcommandnumber)
            self.backoff = 0
            self.commandgap = 1
            backoff_thread = Thread(target=self.update_backoff)
            backoff_event.clear()
            backoff_thread.start()
            self.logger.write("State", "Now leader..")
            
    def unbecome_leader(self):
        """stop being a leader and change type to NODE_REPLICA"""
        # fail-stop tolerance, coupled with retries in the client, mean that a 
        # leader can at any time discard all of its internal state and the protocol
        # will still work correctly.
        self.type = NODE_REPLICA
        backoff_event.set()

    def update_backoff(self):
        """used by the backoffthread to decrease the backoff amount by half periodically"""
        while not backoff_event.isSet():
            self.backoff = self.backoff/2
            backoff_event.wait(BACKOFFDECREASETIMEOUT)

    def detect_colliding_leader(self,ballotnumber):
        """detects a colliding leader from the highest ballotnumber received from acceptors"""
        otherleader_addr,otherleader_port = ballotnumber[BALLOTNODE].split(":")
        otherleader = Peer(otherleader_addr, int(otherleader_port), NODE_LEADER)
        return otherleader
            
    def find_leader(self):
        """returns the minimum peer as the leader"""
        minpeer =  self.me
        if len(self.groups[NODE_REPLICA].members) > 0:
            if self.groups[NODE_REPLICA].members[0] < minpeer:
                minpeer = self.groups[NODE_REPLICA].members[0]
        return minpeer
        
    def check_leader_promotion(self):
        """checks which node is the leader and changes the state of the caller if necessary"""
        chosenleader = self.find_leader()
        if self.me == chosenleader:
            # I need to become a leader
            if self.type != NODE_LEADER:
                # check to see if I have full history
                if self.stateuptodate:
                    self.logger.write("State", "becoming leader")
                    self.become_leader()
                else:
                    self.logger.write("State", "initializing leader")
                    self.become_leader()
                    self.leader_initializing = True
        elif self.type == NODE_LEADER:
            # there is someone else who should act as a leader
            self.logger.write("State", "unbecoming leader")
            self.unbecome_leader()

    def update_ballotnumber(self,seedballotnumber):
        """update the ballotnumber with a higher value than the given ballotnumber"""
        temp = (seedballotnumber[BALLOTNO]+1,self.ballotnumber[BALLOTNODE])
        self.ballotnumber = temp

    def find_commandnumber(self):
        """returns the first gap in proposals and decisions combined"""
        while self.commandgap <= len(self.usedcommandnumbers):
            if self.commandgap in self.usedcommandnumbers:
                self.commandgap += 1
            else:
                return self.commandgap
        return self.commandgap

    def add_to_executed(self, key, value):
        self.executed[key] = value

    def add_to_decisions(self, key, value):
        self.decisions[key] = value
        self.decidedcommandset.add(value)
        self.usedcommandnumbers.add(key)

    def add_to_proposals(self, key, value):
        self.proposals[key] = value
        self.usedcommandnumbers.add(key)

    def add_to_pendingcommands(self, key, value):
        self.pendingcommands[key] = value

    def remove_from_executed(self, key):
        del self.executed[key]

    def remove_from_decisions(self, key):
        self.decidedcommandset.remove(self.decisions[key])
        del self.decisions[key]
        self.usedcommandnumbers.remove(key)

    def remove_from_proposals(self, key):
        del self.proposals[key]
        self.usedcommandnumbers.remove(key)

    def remove_from_pendingcommands(self, key):
        del self.pendingcommands[key]

    def handle_client_command(self, givencommand, prepare=False):
        """handle the received client request
        - if it has been received before check if it has been executed
        -- if it has been executed send the result
        -- if it has not been executed yet send INPROGRESS
        - if this request has not been received before initiate a paxos round for the command"""
        if self.type != NODE_LEADER:
            self.logger.write("State", "got a request but not a leader.")
            return
        
        if self.receivedclientrequests.has_key((givencommand.client, givencommand.clientcommandnumber)):
            self.logger.write("State", "client request received previously")
            self.logger.write("State", "Client: %s Commandnumber: %s\nAcceptors: %s" % (str(givencommand.client), str(givencommand.clientcommandnumber), str(self.groups[NODE_ACCEPTOR])))
            resultsent = False
            # Check if the request has been executed
            if givencommand in self.decidedcommandset:
                if self.executed.has_key(givencommand):
                    clientreply = ClientReplyMessage(MSG_CLIENTREPLY, self.me, reply=self.executed[givencommand][RESULT], \
                                                     replycode=self.executed[givencommand][RCODE], inresponseto=givencommand.clientcommandnumber)
                    self.logger.write("State", "Sending clientreply: %s" % str(clientreply))
                    conn = self.clientpool.get_connection_by_peer(givencommand.client)
                    if conn is not None:
                        conn.send(clientreply)
                    resultsent = True
            # If request not executed yet, send INPROGRESS
            if not resultsent:
                clientreply = ClientReplyMessage(MSG_CLIENTREPLY, self.me, replycode=CR_INPROGRESS, inresponseto=givencommand.clientcommandnumber)
                self.logger.write("State", "Clientreply: %s\nAcceptors: %s" % (str(clientreply),str(self.groups[NODE_ACCEPTOR])))
                conn = self.clientpool.get_connection_by_peer(givencommand.client)
                if conn is not None:
                    conn.send(clientreply)
        else:
            self.receivedclientrequests[(givencommand.client,givencommand.clientcommandnumber)] = givencommand
            self.logger.write("State", "initiating a new command")
            self.logger.write("State", "leader is active: %s" % self.active)
            if self.active and not prepare:
                self.do_command_propose(givencommand)
            else:
                self.do_command_prepare(givencommand)

    def msg_clientrequest(self, conn, msg):
        """handles the request from the client if the node is a leader
        - if not leader: reject
        - if leader: add connection to client connections and handle request"""
        #starttimer(msg.command, 1)
        self.check_leader_promotion()
        self.callsfromclient += 1
        if self.type != NODE_LEADER:
            self.logger.write("State", "not leader.. request rejected..")
            clientreply = ClientReplyMessage(MSG_CLIENTREPLY, self.me, replycode=CR_REJECTED, inresponseto=msg.command.clientcommandnumber)
            self.logger.write("State", "Clientreply: %s\nAcceptors: %s" % (str(clientreply),str(self.groups[NODE_ACCEPTOR])))
            conn.send(clientreply)
            # Check the Leader to see if the Client had a reason to think that we are the leader
            self.ping_leader_once()
            return
        # Leader should accept a request even if it's not ready as this way it will make itself ready during the prepare stage.
        if self.type == NODE_LEADER:
            self.clientpool.add_connection_to_peer(msg.source, conn)
            if self.leader_initializing:
                self.handle_client_command(msg.command, prepare=True)
            else:
                self.handle_client_command(msg.command)

    def msg_incclientrequest(self, conn, msg):
        """handles inconsistent requests from the client"""
        if self.callsfromclient == 0:
            starttimer("A", 1)
        self.callsfromclient += 1
        command = msg.command
        commandlist = command.command.split()
        commandname = commandlist[0]
        commandargs = commandlist[1:]
        send_result_to_client = True
        try:
            method = getattr(self.object, commandname)
            try:
                givenresult = method(*commandargs, _concoord_command=command)
                clientreplycode = CR_OK
                send_result_to_client = True
            except BlockingReturn as blockingretexp:
                givenresult = blockingretexp.returnvalue
                clientreplycode = CR_BLOCK
                send_result_to_client = True
            except UnblockingReturn as unblockingretexp:
                # Get the information about the method call
                # These will be used to update executed and
                # to send reply message to the caller client
                givenresult = unblockingretexp.returnvalue
                unblocked = unblockingretexp.unblocked
                clientreplycode = CR_OK
                send_result_to_client = True
                # If there are clients to be unblocked that have
                # been blocked previously send them unblock messages
                for unblockedclientcommand in unblocked.iterkeys():
                    self.send_reply_to_client(CR_UNBLOCK, None, unblockedclientcommand)
            except Exception as e:
                givenresult = e
                clientreplycode = CR_EXCEPTION
                send_result_to_client = True
                unblocked = {}
        except (TypeError, AttributeError) as t:
            self.logger.write("Execution Error", "command not supported: %s" % (command))
            givenresult = 'COMMAND NOT SUPPORTED'
            clientreplycode = CR_EXCEPTION
            unblocked = {}
            send_result_to_client = True
        if commandname not in METACOMMANDS and send_result_to_client:
            self.send_reply_to_client(clientreplycode, givenresult, command)

    def throughput_test(self):
        self.throughput_runs += 1
        if self.throughput_runs == 1:
            self.throughput_start = time.time()
        elif self.throughput_runs == 30001:
            self.throughput_stop = time.time()
            totaltime = self.throughput_stop - self.throughput_start
            print "********************************************"
            print "TOTAL: ", totaltime
            print "TPUT: ", 30000/totaltime, "req/s"
            print "********************************************"
            sys.stdout.flush()
            os._exit(1)
        command = Command(client=self.me, clientcommandnumber=random.randint(1,10000000), command='noop')
        self.handle_client_command_tput(command)
            
    def msg_clientreply(self, conn, msg):
        """this only occurs in response to commands initiated by the shell"""
        print "==================>", msg

    def msg_output(self, conn, msg):
        profile_off()
        profilerdict = get_profile_stats()
        for key, value in sorted(profilerdict.iteritems(), key=lambda (k,v): (v[2],k)):
            print "%s: %s" % (key, value)
        time.sleep(10)
        sys.stdout.flush()
        dumptimers(str(len(self.groups[NODE_REPLICA])+1), str(len(self.groups[NODE_ACCEPTOR])), self.type)

    # Paxos Methods
    def do_command_propose_frompending(self, givencommandnumber):
        """initiates the givencommandnumber from pendingcommands list
        removes the command from pending and transfers it to proposals
        if there are no acceptors present, sets the lists back and returns"""
        givenproposal = self.pendingcommands[givencommandnumber]
        self.remove_from_pendingcommands(givencommandnumber)
        self.add_to_proposals(givencommandnumber, givenproposal)
        recentballotnumber = self.ballotnumber
        self.logger.write("State", "proposing command: %d:%s with ballotnumber %s and %d acceptors" % (givencommandnumber,givenproposal,str(recentballotnumber),len(self.groups[NODE_ACCEPTOR])))
        # since we never propose a commandnumber that is beyond the window, we can simply use the current acceptor set here
        prc = ResponseCollector(self.groups[NODE_ACCEPTOR], recentballotnumber, givencommandnumber, givenproposal)
        if len(prc.acceptors) == 0:
            self.logger.write("System Error", "There are no acceptors!")
            self.remove_from_proposals(givencommandnumber)
            self.add_to_pendingcommands(givencommandnumber, givenproposal)
            return
        self.outstandingproposes[givencommandnumber] = prc
        propose = PaxosMessage(MSG_PROPOSE,self.me,recentballotnumber,commandnumber=givencommandnumber,proposal=givenproposal)
        msgids = self.send(propose,group=prc.acceptors)
        # add sent messages to the sent proposes
        prc.sent.extend(msgids)
        
    def do_command_propose(self, givenproposal):
        """propose a command with the given commandnumber and proposal. Stage p2a.
        A command is proposed by running the PROPOSE stage of Paxos Protocol for the command.
        """
        if self.type != NODE_LEADER:
            self.logger.write("State", "Ignoring received propose: not a leader.")
            return
        givencommandnumber = self.find_commandnumber()
        self.add_to_pendingcommands(givencommandnumber, givenproposal)
        # if we're too far in the future, i.e. past window, do not issue the command
        if givencommandnumber - self.nexttoexecute >= WINDOW:
            return
        self.do_command_propose_frompending(givencommandnumber)
            
    def do_command_prepare_frompending(self, givencommandnumber):
        """initiates the givencommandnumber from pendingcommands list
        removes the command from pending and transfers it to proposals
        if there are no acceptors present, sets the lists back and returns"""
        givenproposal = self.pendingcommands[givencommandnumber]
        self.remove_from_pendingcommands(givencommandnumber)
        self.add_to_proposals(givencommandnumber, givenproposal)
        newballotnumber = self.ballotnumber
        self.logger.write("State", "preparing command: %d:%s with ballotnumber %s" % (givencommandnumber, givenproposal,str(newballotnumber)))
        prc = ResponseCollector(self.groups[NODE_ACCEPTOR], newballotnumber, givencommandnumber, givenproposal)
        if len(prc.acceptors) == 0:
            print "There are no acceptors."
            self.remove_from_proposals(givencommandnumber)
            self.add_to_pendingcommands(givencommandnumber, givenproposal)
            return
        self.outstandingprepares[newballotnumber] = prc
        prepare = PaxosMessage(MSG_PREPARE,self.me,newballotnumber)
        msgids = self.send(prepare,group=prc.acceptors)
        # add sent messages to the sent prepares
        prc.sent.extend(msgids)

    def do_command_prepare(self, givenproposal):
        """Prepare a command with the given commandnumber and proposal. Stage p1a.
        A command is initiated by running a Paxos Protocol for the command.

        State Updates:
        - Start from the PREPARE STAGE:
        -- create MSG_PREPARE: message carries the corresponding ballotnumber
        -- create ResponseCollector object for PREPARE STAGE: ResponseCollector keeps
        the state related to MSG_PREPARE
        -- add the ResponseCollector to the outstanding prepare set
        -- send MSG_PREPARE to Acceptor nodes
        """
        if self.type != NODE_LEADER:
            print "Not a Leader.."
            return

        givencommandnumber = self.find_commandnumber()
        self.add_to_pendingcommands(givencommandnumber, givenproposal)
        # if we're too far in the future, i.e. past window, do not issue the command
        if givencommandnumber - self.nexttoexecute >= WINDOW:
            return
        self.do_command_prepare_frompending(givencommandnumber)
            
    def msg_prepare_adopted(self, conn, msg):
        """MSG_PREPARE_ADOPTED is handled only if it belongs to an outstanding MSG_PREPARE,
        otherwise it is discarded.
        When MSG_PREPARE_ADOPTED is received, the corresponding ResponseCollector is retrieved
        and its state is updated accordingly.

        State Updates:
        - message is added to the received dictionary
        - the pvalue with the ResponseCollector's commandnumber is added to the possiblepvalueset
        - if naccepts is greater than the quorum size PREPARE STAGE is successful.
        -- Start the PROPOSE STAGE:
        --- create the pvalueset with highest ballotnumbers for distinctive commandnumbers
        --- update own proposals dictionary according to pmax dictionary
        --- remove the old ResponseCollector from the outstanding prepare set
        --- run the PROPOSE STAGE for each pvalue in proposals dictionary
        ---- create ResponseCollector object for PROPOSE STAGE: ResponseCollector keeps
        the state related to MSG_PROPOSE
        ---- add the new ResponseCollector to the outstanding propose set
        ---- create MSG_PROPOSE: message carries the corresponding ballotnumber, commandnumber and the proposal
        ---- send MSG_PROPOSE to the same Acceptor nodes from the PREPARE STAGE
        """
        if self.outstandingprepares.has_key(msg.inresponseto):
            prc = self.outstandingprepares[msg.inresponseto]
            prc.received[msg.source] = msg
            self.logger.write("Paxos State", "got an accept for ballotno %s commandno %s proposal %s with %d out of %d" % (prc.ballotnumber, prc.commandnumber, prc.proposal, len(prc.received), prc.ntotal))
            assert msg.ballotnumber == prc.ballotnumber, "[%s] MSG_PREPARE_ADOPTED can't have non-matching ballotnumber" % self
            # add all the p-values from the response to the possiblepvalueset
            if msg.pvalueset is not None:
                prc.possiblepvalueset.union(msg.pvalueset)

            if len(prc.received) >= prc.nquorum:
                self.logger.write("Paxos State", "suffiently many accepts on prepare!")
                # delete outstanding messages that I don't need to check for anymore
                for msgid in prc.sent:
                    if self.outstandingmessages.has_key(msgid):
                        del self.outstandingmessages[msgid]
                del self.outstandingprepares[msg.inresponseto]
                # choose pvalues with distinctive commandnumbers and highest ballotnumbers
                pmaxset = prc.possiblepvalueset.pmax()
                for commandnumber,proposal in pmaxset.iteritems():
                    self.add_to_proposals(commandnumber, proposal)
                # If the commandnumber we were planning to use is overwritten
                # we should try proposing with a new commandnumber
                if self.proposals[prc.commandnumber] != prc.proposal:
                    self.do_command_propose(prc.proposal)
                for chosencommandnumber,chosenproposal in self.proposals.iteritems():
                    self.logger.write("Paxos State", "Sending PROPOSE for %d, %s" % (chosencommandnumber, chosenproposal))
                    newprc = ResponseCollector(prc.acceptors, prc.ballotnumber, chosencommandnumber, chosenproposal)
                    self.outstandingproposes[chosencommandnumber] = newprc
                    propose = PaxosMessage(MSG_PROPOSE,self.me,prc.ballotnumber,commandnumber=chosencommandnumber,proposal=chosenproposal)
                    self.send(propose,group=newprc.acceptors)
                # As leader collected all proposals from acceptors its state is up-to-date and it is done initializing
                self.leader_initializing = False
                self.stateuptodate = True
                # become active
                self.active = True

    def msg_prepare_preempted(self, conn, msg):
        """MSG_PREPARE_PREEMPTED is handled only if it belongs to an outstanding MSG_PREPARE,
        otherwise it is discarded.
        A MSG_PREPARE_PREEMPTED causes the PREPARE STAGE to be unsuccessful, hence the current
        state is deleted and a ne PREPARE STAGE is initialized.

        State Updates:
        - kill the PREPARE STAGE that received a MSG_PREPARE_PREEMPTED
        -- remove the old ResponseCollector from the outstanding prepare set
        - remove the command from proposals, add it to pendingcommands
        - update the ballotnumber
        - call do_command_prepare() to start a new PREPARE STAGE:
        """
        if self.outstandingprepares.has_key(msg.inresponseto):
            prc = self.outstandingprepares[msg.inresponseto]
            self.logger.write("Paxos State", "got a reject for ballotno %s commandno %s proposal %s with %d out of %d" % (prc.ballotnumber, prc.commandnumber, prc.proposal, len(prc.received), prc.ntotal))
            # delete outstanding messages that I don't need to check for anymore
            for msgid in prc.sent:
                if self.outstandingmessages.has_key(msgid):
                    del self.outstandingmessages[msgid]
            # take this response collector out of the outstanding prepare set
            del self.outstandingprepares[msg.inresponseto]
            # become inactive
            self.active = False
            # update the ballot number
            self.update_ballotnumber(msg.ballotnumber)
            self.remove_from_proposals(prc.commandnumber)
            self.add_to_pendingcommands(prc.commandnumber, prc.proposal)
            # backoff -- we're holding the node lock, so no other state machine code can make progress
            leader_causing_reject = self.detect_colliding_leader(msg.ballotnumber)
            if leader_causing_reject < self.me:
                # if I lost to someone whose name precedes mine, back off more than he does
                self.backoff += BACKOFFINCREASE
            time.sleep(self.backoff)
            self.do_command_prepare(prc.proposal)

    def msg_propose_accept(self, conn, msg):
        """MSG_PROPOSE_ACCEPT is handled only if it belongs to an outstanding MSG_PREPARE,
        otherwise it is discarded.
        When MSG_PROPOSE_ACCEPT is received, the corresponding ResponseCollector is retrieved
        and its state is updated accordingly.

        State Updates:
        - message is added to the received dictionary
        - if length of received is greater than the quorum size, PROPOSE STAGE is successful.
        -- remove the old ResponseCollector from the outstanding prepare set
        -- create MSG_PERFORM: message carries the chosen commandnumber and proposal.
        -- send MSG_PERFORM to all Replicas and Leaders
        -- execute the command
        """
        if self.outstandingproposes.has_key(msg.commandnumber):
            prc = self.outstandingproposes[msg.commandnumber]
            if msg.inresponseto == prc.ballotnumber:
                prc.received[msg.source] = msg
                self.logger.write("Paxos State", "got an accept for proposal ballotno %s commandno %s proposal %s making %d out of %d accepts" % \
                       (prc.ballotnumber, prc.commandnumber, prc.proposal, len(prc.received), prc.ntotal))
                if len(prc.received) >= prc.nquorum:
                    self.logger.write("Paxos State", "Agreed on %s" % prc.proposal) 
                    # take this response collector out of the outstanding propose set
                    self.add_to_proposals(prc.commandnumber, prc.proposal)
                    # delete outstanding messages that I don't need to check for anymore
                    for msgid in prc.sent:
                        if self.outstandingmessages.has_key(msgid):
                            del self.outstandingmessages[msgid]
                    del self.outstandingproposes[msg.commandnumber]
                    # now we can perform this action on the replicas
                    performmessage = PaxosMessage(MSG_PERFORM,self.me,commandnumber=prc.commandnumber,proposal=prc.proposal)
                    try:
                        self.logger.write("Paxos State", "Sending PERFORM!")
                        self.send(performmessage, group=self.groups[NODE_REPLICA])
                        self.send(performmessage, group=self.groups[NODE_LEADER])
                        self.send(performmessage, group=self.groups[NODE_NAMESERVER])
                    except:
                        pass
                    self.perform(performmessage, designated=True)
        
    def msg_propose_reject(self, conn, msg):
        """MSG_PROPOSE_REJECT is handled only if it belongs to an outstanding MSG_PROPOSE,
        otherwise it is discarded.
        A MSG_PROPOSE_REJECT causes the PROPOSE STAGE to be unsuccessful, hence the current
        state is deleted and a new PREPARE STAGE is initialized.

        State Updates:
        - kill the PROPOSE STAGE that received a MSG_PROPOSE_REJECT
        -- remove the old ResponseCollector from the outstanding prepare set
        - remove the command from proposals, add it to pendingcommands
        - update the ballotnumber
        - call do_command_prepare() to start a new PREPARE STAGE:
        """
        if self.outstandingproposes.has_key(msg.commandnumber):
            prc = self.outstandingproposes[msg.commandnumber]
            if msg.inresponseto == prc.ballotnumber:
                self.logger.write("Paxos State", "got a reject for proposal ballotno %s commandno %s proposal %s still %d out of %d accepts" % \
                       (prc.ballotnumber, prc.commandnumber, prc.proposal, len(prc.received), prc.ntotal))
                # delete outstanding messages that I don't need to check for anymore
                for msgid in prc.sent:
                    if self.outstandingmessages.has_key(msgid):
                        del self.outstandingmessages[msgid]
                # take this response collector out of the outstanding propose set
                del self.outstandingproposes[msg.commandnumber]
                # become inactive
                self.active = False
                # update the ballot number
                self.update_ballotnumber(msg.ballotnumber)
                # remove the proposal from proposal
                self.remove_from_proposals(prc.commandnumber)
                self.add_to_pendingcommands(prc.commandnumber, prc.proposal)
                leader_causing_reject = self.detect_colliding_leader(msg.ballotnumber)
                if leader_causing_reject < self.me:
                    # if I lost to someone whose name precedes mine, back off more than he does
                    self.backoff += BACKOFFINCREASE
                self.logger.write("Paxos State", "There is another leader, backing off.")
                time.sleep(self.backoff)
                self.do_command_prepare(prc.proposal)

    def ping_leader(self):
        """used by the ping_thread to ping the current leader periodically"""
        while True:
            currentleader = self.find_leader()
            if currentleader != self.me:
                self.logger.write("State", "Sending PING to %s" % currentleader)
                pingmessage = HandshakeMessage(MSG_PING, self.me)
                try:
                    self.send(pingmessage, peer=currentleader)
                except:
                    self.logger.write("State", "Leader not responding, removing current leader from the replicalist")
                    self.groups[NODE_REPLICA].remove(currentleader)
            time.sleep(LIVENESSTIMEOUT)

    def ping_leader_once(self):
        currentleader = self.find_leader()
        if currentleader != self.me:
            self.logger.write("State", "Sending PING to %s" % currentleader)
            helomessage = HandshakeMessage(MSG_HELO, self.me)
            try:
                self.send(helomessage, peer=currentleader)
            except:
                self.logger.write("State", "Leader not responding, removing current leader from the replicalist")
                self.groups[NODE_REPLICA].remove(currentleader)

    def msg_helo(self, conn, msg):
        # This is the first acceptor, it has to be added by this replica
        if msg.source.type == NODE_ACCEPTOR and len(self.groups[NODE_ACCEPTOR]) == 0:
            self.groups[msg.source.type].add(msg.source)
            # Agree with other Replicas about adding this acceptor
            addcommand = self.create_add_command(msg.source)
            self.check_leader_promotion()
            self.do_command_prepare(addcommand)
        else:
            addcommand = self.create_add_command(msg.source)
            self.do_command_prepare(addcommand)

    def create_delete_command(self, node):
        mynumber = self.metacommandnumber
        self.metacommandnumber += 1
        operation = "_del_node %d %s:%d" % (node.type, node.addr, node.port)
        command = Command(self.me, mynumber, operation)
        return command

    def create_add_command(self, node):
        mynumber = self.metacommandnumber
        self.metacommandnumber += 1
        operation = "_add_node %d %s:%d" % (node.type, node.addr, node.port)
        command = Command(self.me, mynumber, operation)
        return command

    # Debug Methods
    def cmd_command(self, args):
        """shell command [command]: initiate a new command.
        This function calls do_command_propose() with inputs from the Shell."""
        try:
            proposal = ' '.join(args[1:])
            cmdproposal = Command(client=self.me, clientcommandnumber=random.randint(1,10000000), command=proposal)
            self.handle_client_command(cmdproposal)
        except IndexError:
            print "command expects only one command"

    def cmd_goleader(self, args):
        """shell command [goleader]: start Leader state""" 
        self.become_leader()

    def cmd_clients(self,args):
        """prints client connections"""
        print self.clientpool

    def cmd_decisions(self,args):
        """prints decisions"""
        for cmdnum,decision in self.decisions.iteritems():
            print "%d: %s" % (cmdnum,str(decision))

    def cmd_executed(self,args):
        """prints decision states"""
        for decision,state in self.executed.iteritems():
            print "%s: %s" % (str(decision),str(state))

    def cmd_proposals(self,args):
        """prints proposals"""
        for cmdnum,command in self.proposals.iteritems():
            print "%d: %s" % (cmdnum,str(command))

    def cmd_pending(self,args):
        """prints pending commands"""
        for cmdnum,command in self.pendingcommands.iteritems():
            print "%d: %s" % (cmdnum,str(command))

    def printlists(self):
        print "-- DECISIONS --"
        for i, j in self.decisions.iteritems():
            print i, "\t", j
        print "-- EXECUTED --"
        for i, j in self.executed.iteritems():
            print i, "\t", j
        print "-- PROPOSALS --"
        for i, j in self.proposals.iteritems():
            print i, "\t", j
        print "-- PENDING COMMANDS --"
        for i, j in self.pendingcommands.iteritems():
            print i, "\t", j
        print "-- USED COMMANDNUMBERS --"
        for i in self.usedcommandnumbers:
            print i

    def terminate_handler(self, signal, frame):
        sys.stdout.flush()
        sys.stderr.flush()
        self.logger.close()
        os._exit(0)

def main():
    replicanode = Replica()
    replicanode.startservice()
    signal.signal(signal.SIGINT, replicanode.terminate_handler)
    signal.signal(signal.SIGTERM, replicanode.terminate_handler)
    signal.pause()
    
if __name__=='__main__':
    main()
