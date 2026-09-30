import sys
from cmp import compare
r,o,fb,err=compare(sys.argv[1],verbose=False)
p=int(sys.argv[2]); w=int(sys.argv[3]) if len(sys.argv)>3 else 30
tr=[t for t in r.trace if t[0]<=p]
for t in tr[-w:]:
    ro=o['reads'].get(t[0])
    print(t[0],t[1],(t[2],t[3]),'ORACLE',ro[:3] if ro else None, t[4][-90:])
print('oracle after:')
for k in sorted(x for x in o['reads'] if p<=x<p+400)[:15]: print(k,o['reads'][k][:3],hex(o['reads'][k][3]))
