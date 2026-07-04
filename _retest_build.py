import time, triage
from harness_client import HarnessClient
c = HarnessClient()
for pfx in ["uriparser___uriparser","ClusterLabs___libqb","OpenIDC___cjose"]:
    bug = next(b for b in c.list_bugs(exclude_substr=None) if b.startswith(pfx))
    t0=time.time()
    h=c.reproduce(bug, force_cleanup=True); c.wait_for_reproduce(h)
    ev=triage.triage(c.read_test_logs(bug))
    ce=(ev.get('compile_error') or {}).get('message','')
    print(f"[{pfx.split('___')[0]:12s}] {time.time()-t0:5.0f}s  class={ev['failure_class']:16s} {'| '+ce[:55] if ce else '| '+ev['summary'][:55]}", flush=True)
print("DONE", flush=True)
