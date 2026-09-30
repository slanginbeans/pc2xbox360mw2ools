import sys
from emu import *
import mw2tex
def cstr(e,a):
    b=bytes(e.u.mem_read(a,200)); return b.split(b'\0')[0].decode('latin1')
MSGS=[]
def pr(e):
    MSGS.append((cstr(e,e.reg(3)),[e.reg(i) for i in (4,5,6)]))
ASSETS=[]
SS=[]
FINDS=[]
def find(e):
    FINDS.append((e.reg(3),cstr(e,e.reg(4)))); return 0x7e004000
def slget(e):
    SS.append(cstr(e,e.reg(3))); return len(SS)
def addasset(e):
    t=e.reg(3); h=struct.unpack('>I',bytes(e.u.mem_read(e.reg(4),4)))[0]
    ASSETS.append((t,h,e.pos))
    e.u.mem_write(0x7e00c000,struct.pack('>II',t,h)); return 0x7e00c000
def run(zone,stubs={}):
    stubs={0x8233d008:pr,0x821de528:addasset,0x822a2e18:slget,0x821e25b0:find,**stubs}
    e=Emu(zone,{0x82281fa0:0,**stubs})
    e.call(0x821e2c70,e.zm)
    e.setreg(30,0x825ce4c0); e.setreg(21,e.zm); e.setreg(23,1); e.setreg(25,0x82440000)
    e.setreg(1,STACK-0x1000); e.setreg(13,0x7e008000)
    e.u.reg_write(UC_PPC_REG_LR,RET)
    try: e.u.emu_start(0x821b0024,0x821b00cc)
    except UcError as ex: e.err=e.err or '%s bt %s'%(ex,e.bt())
    return e
if __name__=='__main__':
    zone=mw2tex.FastFile(sys.argv[1]).zone
    e=run(zone)
    print(MSGS[:5]);print(len(ASSETS),ASSETS[:5]);print('err',e.err,'pos',e.pos,'of',len(zone),'reads',len(e.trace),{hex(k):v for k,v in e.imports.items()})
