import sys,struct,os
import tu6
from unicorn import *
from unicorn.ppc_const import *
B=0x82000000
PE=open(tu6.pe_path(),'rb').read()
IMG=0x1e40000
READ=0x821b0b10; LOADSTREAM=0x821e2e98; XSTR=0x821e2f60
STACK=0x70000000; HEAP=0x10000000
RET=0x7f000000  # magic return address

def patched():
    img=bytearray(PE[:IMG])
    T0,T1=0x820a0000-B,0x820a0000+0x38e5a4-B
    n=0
    for o in range(T0,T1,4):
        w=struct.unpack_from('>I',img,o)[0]; op=w>>26
        if op==31 and ((w>>1)&0x3ff) in (83,178,146):
            struct.pack_into('>I',img,o,0x60000000); continue
        if op in (58,62) and (w&3) in (0,1):
            d=(w&0xfffc)
            if d&0x8000: d-=0x10000
            d+=4
            if -0x8000<=d<0x8000:
                nop=(36 if op==62 else 32)+(w&1)   # stw/stwu , lwz/lwzu
                w2=(nop<<26)|(w&0x03ff0000)|(d&0xffff)
                struct.pack_into('>I',img,o,w2); n+=1
    return bytes(img)
class Emu:
    def __init__(s, zone, stubs=None, trace=True):
        s.zone=zone; s.pos=32; s.trace=[] ; s.dotrace=trace
        u=s.u=Uc(UC_ARCH_PPC, UC_MODE_PPC32|UC_MODE_BIG_ENDIAN)
        u.mem_map(B,IMG); u.mem_write(B,patched())
        u.mem_map(STACK-0x100000,0x100000+0x10000)
        u.mem_map(0x7e000000,0x10000); u.mem_write(0x7e008100,struct.pack('>I',0x7e000000)); u.mem_write(0x7e008000,struct.pack('>I',0x7e001000)); u.mem_map(0x60000000,0x8000000); u.mem_write(0x826794fc,struct.pack('>II',0x60000000,0)); u.mem_write(0x82442580,struct.pack('>I',1)); u.mem_write(0x8292258c,struct.pack('>I',0)); u.mem_map(RET,0x1000); u.mem_write(RET,b'\x48\x00\x00\x00')  # b . (infinite loop) we stop on hook
        hdr=struct.unpack_from('>8I',zone,0)
        s.blocksizes=hdr[2:]
        s.blocks=[]; a=HEAP
        for sz in s.blocksizes:
            sz2=(sz+0xffff)&~0xffff or 0x10000
            u.mem_map(a,sz2+0x10000); s.blocks.append(a); a+=sz2+0x20000
        s.zm=a; u.mem_map(a,0x10000)
        for i,b in enumerate(s.blocks): u.mem_write(a+i*8,struct.pack('>II',b,s.blocksizes[i]))
        s.stubs=dict(stubs or {})
        u.hook_add(UC_HOOK_CODE,s._read,begin=READ,end=READ)
        u.hook_add(UC_HOOK_CODE,s._ret,begin=RET,end=RET)
        for a in s.stubs: u.hook_add(UC_HOOK_CODE,s._stub,begin=a,end=a)
        u.hook_add(UC_HOOK_MEM_UNMAPPED|UC_HOOK_MEM_FETCH_UNMAPPED,s._bad)
        s.err=None; s.imports={}
        u.hook_add(UC_HOOK_CODE,s._imp,begin=0x8242d8b4,end=0x8242e5a4)
    def reg(s,n): return s.u.reg_read(UC_PPC_REG_0+n)&0xffffffff
    def setreg(s,n,v): s.u.reg_write(UC_PPC_REG_0+n,v&0xffffffff)
    def _ret_to_lr(s):
        s.u.reg_write(UC_PPC_REG_PC,s.u.reg_read(UC_PPC_REG_LR)&0xffffffff)
    def _read(s,u,addr,size,ud):
        dst=s.reg(3); n=s.reg(4)
        data=s.zone[s.pos:s.pos+n]
        if len(data)<n:
            s.err='read past end at %d (+%d)'%(s.pos,n); u.emu_stop(); return
        u.mem_write(dst,bytes(data))
        if s.dotrace: s.trace.append((s.pos,n,u.reg_read(UC_PPC_REG_LR)&0xffffffff,dst))
        s.pos+=n
        s._ret_to_lr()
    def _stub(s,u,addr,size,ud):
        r=s.stubs[addr]
        if callable(r): r=r(s)
        if r is not None: s.setreg(3,r)
        s._ret_to_lr()
    def _imp(s,u,addr,size,ud):
        base=addr-((addr-0x8242d8b4)%16)
        w=struct.unpack('>I',bytes(u.mem_read(base,4)))[0]
        s.imports[w]=s.imports.get(w,0)+1
        s.setreg(3,0); s._ret_to_lr()
    def bt(s):
        out=[s.u.reg_read(UC_PPC_REG_PC)&0xffffffff, s.u.reg_read(UC_PPC_REG_LR)&0xffffffff]
        sp=s.reg(1)
        for i in range(30):
            try:
                prev=struct.unpack('>I',bytes(s.u.mem_read(sp,4)))[0]
                if not prev or prev>=STACK: break
                out.append(struct.unpack('>I',bytes(s.u.mem_read(prev-8,4)))[0]); sp=prev
            except UcError: break
        return ' '.join('%x'%x for x in out)
    def _ret(s,u,addr,size,ud): u.emu_stop()
    def _bad(s,u,access,addr,size,value,ud):
        s.err='bad mem access %x bt %s'%(addr,s.bt()); return False
    def call(s,fn,*args):
        for i,a in enumerate(args): s.setreg(3+i,a)
        s.setreg(1,STACK-0x1000); s.setreg(13,0x7e008000)
        s.u.reg_write(UC_PPC_REG_LR,RET)
        try: s.u.emu_start(fn,RET+0x100)
        except UcError as e:
            if not s.err: s.err='%s bt %s'%(e,s.bt())
        return s.reg(3)
