#automatically generated by the proxygenerator
from clientproxy import *

class Test():
    def __init__(self, bootstrap):
        self.proxy = ClientProxy(bootstrap)

    def getvalue(self):
        self.proxy.invoke_command("getvalue", )

    def __str__(self):
        self.proxy.invoke_command("__str__", )

