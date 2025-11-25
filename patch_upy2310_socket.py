import socket as _socket

AF_INET = _socket.AF_INET
SOCK_STREAM = _socket.SOCK_STREAM

# connect() sometimes hangs forever on versions <= 2310.

class socket(_socket.socket):
    def connect(self, addr):
        import select
        from errno import EINPROGRESS
        _sock_connect_timeout:int = 5*1000 #ms

        poller = select.poll()
        poller.register(self, select.POLLIN | select.POLLOUT)

        try:
            super().connect(addr)
        except OSError as e:
            if e.errno != EINPROGRESS:
                raise e

        #self.dprint("- poll_fix: polling sock for connect open")
        res = poller.poll(_sock_connect_timeout)
        #print("c2u", res)
        poller.unregister(self)
        #print("c2ud", res)
        if not res:
            #print("c2e", res)
            self.close()
            raise OSError('Socket Connect Timeout')

def getaddrinfo(server, port, flag_type1, flag_type2 ):
    return _socket.getaddrinfo(server, port, flag_type1, flag_type2)

def inet_ntop(addr1, addr2):
    return _socket.inet_ntop(addr1, addr2)
