import time
from harness_client import HarnessClient
import asan_parse
c=HarnessClient()
bug=next(b for b in c.list_bugs() if b.startswith("Yeraze___ytnef"))
t0=time.time()
h=c.reproduce(bug, sanitize="address", force_cleanup=True)
final=c.wait_for_reproduce(h)
diag=asan_parse.parse_log(c.read_test_logs(bug))
print(f"[ytnef] bug={bug.split('@')[1][:8]} status={final.get('status')} elapsed={time.time()-t0:.0f}s trace={'YES' if diag else 'no'}")
if diag: print("[ytnef]", diag)
print("[ytnef] DONE")
