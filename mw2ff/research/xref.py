import tu6
import struct,sys,pickle,os
B=0x82000000
pe=open(tu6.pe_path(),'rb').read()
T0,T1=0x820a0000,0x820a0000+0x38e5a4
def build():
    words=struct.unpack_from('>%dI'%((T1-T0)//4),pe,T0-B)
    refs={}; calls={}
    lis={}
    for i,w in enumerate(words):
        a=T0+i*4; op=w>>26
        if op==15:
            d=(w>>21)&31; s=(w>>16)&31; imm=w&0xffff
            if s==0: lis[d]=(imm<<16)&0xffffffff; continue
        if op in (14,32,34,36,38,40,44,48,52,24):  # addi, lwz,lbz,stw,stb,lhz,sth,lfs,stfs, ori
            d=(w>>21)&31; s=(w>>16)&31; imm=w&0xffff
            if s in lis:
                v=(lis[s]+ (imm if op==24 else (imm-0x10000 if imm&0x8000 else imm)))&0xffffffff
                refs.setdefault(v,[]).append(a)
            if op in (14,32,34,40,24) and d in lis and d!=s: del lis[d]
        elif op==18:
            li=w&0x3fffffc
            if li&0x2000000: li-=0x4000000
            t=(li if w&2 else a+li)&0xffffffff
            calls.setdefault(t,[]).append((a,w&1))
    return refs,calls
P=os.path.join(tu6.CACHE,'xref.pkl')
if os.path.exists(P): refs,calls=pickle.load(open(P,'rb'))
else:
    refs,calls=build(); pickle.dump((refs,calls),open(P,'wb'))
def func_start(a):
    while a>B:
        a-=4
        if pe[a-B:a-B+4]==bytes.fromhex('7d8802a6'): return a
if __name__=='__main__':
    for x in sys.argv[1:]:
        t=int(x,16)
        print(x,'refs',[hex(r)+'@'+hex(func_start(r) or 0) for r in refs.get(t,[])][:20])
        print(x,'calls',[hex(c)+'@'+hex(func_start(c) or 0) for c,l in calls.get(t,[])][:30])
