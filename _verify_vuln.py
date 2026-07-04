import time
from harness_client import HarnessClient
import asan_parse
c = HarnessClient()
bug = next(b for b in c.list_bugs(exclude_substr=None) if b.startswith("DaveGamble___cJSON"))
print("[cjson] bug:", bug, flush=True)
t0=time.time()
h = c.reproduce(bug, force_cleanup=True)   # plain reproduce — project builds with ASan itself
final = c.wait_for_reproduce(h)
el=time.time()-t0
logs = c.read_test_logs(bug)
diag = asan_parse.parse_log(logs)
print(f"[cjson] status={final.get('status')} elapsed={el:.0f}s  log_bytes={len(logs)}", flush=True)
print(f"[cjson] ASan/UBSan trace parsed: {'YES' if diag else 'no'}", flush=True)
if diag:
    print("[cjson]", asan_parse.to_prompt_block(diag), flush=True)
else:
    # show raw sanitizer-ish lines if any
    import re
    hits=[l for l in logs.splitlines() if re.search(r'AddressSanitizer|==ERROR|runtime error|SEGV|overflow',l)]
    print("[cjson] raw sanitizer lines:", hits[:5], flush=True)
print("[cjson] DONE", flush=True)
