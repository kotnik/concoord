'''
@author: denizalti
@note: The Client
@date: February 1, 2011
'''
import socket
from optparse import OptionParser
from threading import Thread, Lock, Condition

from node import Node
from enums import *
from utils import findOwnIP
from connection import ConnectionPool, Connection
from group import Group
from peer import Peer
from message import ClientMessage,Message,PaxosMessage,HandshakeMessage,AckMessage,PValue,PValueSet, Command
import os
import time

parser = OptionParser(usage="usage: %prog -b bootstrap")
parser.add_option("-b", "--boot", action="store", dest="bootstrap", help="address:port:type triple for the bootstrap peer")
(options, args) = parser.parse_args()

class Client():
    """Client sends requests and receives responses"""
    def __init__(self, bootstrap):
        """Initialize Client

        Client State
        - socket: socket of Client
        - me: Peer instance of Client
        - conn: Connection on Client's socket
        - alive: liveness of Client
        """
        self.socket = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        bootaddr,bootport = bootstrap.split(":")
        self.socket.connect((bootaddr,int(bootport)))
        myaddr = findOwnIP()
        myport = self.socket.getsockname()[1]
        self.me = Peer(myaddr,myport,NODE_CLIENT)
        self.conn = Connection(self.socket)
        self.alive = True
        self.clientcommandnumber = 1
        
    def clientloop(self):
        """Accepts commands from the prompt and sends requests for the commands
        and receives corresponding replies.
        """
        while self.alive:
            try:
                shellinput = raw_input("client-shell> ")
                print shellinput
                if len(shellinput) == 0:
                    continue
                else:
                    mynumber = self.clientcommandnumber
                    self.clientcommandnumber += 1
                    
                    command = Command(self.me, mynumber, shellinput)
                    cm = ClientMessage(MSG_CLIENTREQUEST, self.me, command)
                    acked = False
                    replied = False
                    print "Client Message about to be sent:", cm
                    starttime = time.time()
                    self.conn.settimeout(CLIENTRESENDTIMEOUT)

                    while not acked or not replied:
                        if not acked:
                            self.conn.send(cm)
                        reply = self.conn.receive()
                        print "---------------------->", reply
                        if time.time() - starttime > CLIENTRESENDTIMEOUT:
                            print "bootstrap node failed to respond in time"
                            break
                        if reply is None:
                            print "receive returned None"
                            break
                        acked = acked or (reply and reply.type == MSG_ACK and reply.ackid == cm.id)
                        if reply and reply.type == MSG_CLIENTREPLY and reply.inresponseto == mynumber:
                            replied = True
                            print reply
            except ( KeyboardInterrupt,EOFError ):
                os._exit(0)
        return
        
theClient = Client(options.bootstrap)
theClient.clientloop()

  


    
