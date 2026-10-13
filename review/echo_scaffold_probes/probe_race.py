import sys,tempfile,shutil,json,multiprocessing as mp,time
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1]); import collector_fixture as C
def w(root,slot,barrier):
    barrier.wait()
    try: return C.SlotStore(Path(root)).record(slot,'SOURCE_FAILED',datetime.now(timezone.utc),reason='race')
    except Exception as e: return 'EXC '+type(e).__name__
if __name__=='__main__':
    hits=0
    for i in range(30):
        root=tempfile.mkdtemp(); m=mp.Manager(); b=m.Barrier(4)
        with mp.Pool(4) as p: res=p.starmap(w,[(root,1,b)]*4)
        try: C.SlotStore(Path(root)).validate(); ok=True
        except Exception: ok=False
        if not ok: hits+=1
    print(json.dumps({'D8_concurrent_trials':30,'ledger_corrupted_trials':hits}))
