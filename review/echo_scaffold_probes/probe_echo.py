import sys,tempfile,shutil,json,multiprocessing as mp
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,sys.argv[1]); import collector_fixture as C
FX=Path(sys.argv[1])/'fixtures'/'coinbase_BTC.raw'
def fresh():
    d=Path(tempfile.mkdtemp()); shutil.copy(FX,d/'coinbase_BTC.raw'); return d
R={}
# D1 backfill: slot 1 written 30 days later accepted
d=fresh(); R['D1_backfill_accepted']=C.fixture_capture(d,1,datetime(2026,11,30,tzinfo=timezone.utc))
# D2 stale real bytes (9 Oct book) accepted as CAPTURED on 13 Oct
d=fresh(); R['D2_stale_bytes_accepted']=C.fixture_capture(d,2,datetime(2026,10,13,tzinfo=timezone.utc))
# D3 real OSError -> crash, no ledger row
d=fresh(); st=C.SlotStore(d/'_test_store'); (d/'_test_store'/'artifacts').mkdir(parents=True); (d/'_test_store'/'artifacts'/'slot_000001.raw').mkdir()
try: st.record(1,'CAPTURED',datetime.now(timezone.utc),FX.read_bytes()); R['D3_oserror']='handled'
except Exception as e: R['D3_oserror']='UNHANDLED '+type(e).__name__+' rows='+str(len(st.records()))
# D4 orphan artifact after crash between artifact write & ledger -> retry bricks slot
d=fresh(); a=d/'_test_store'/'artifacts'; a.mkdir(parents=True); (a/'slot_000005.raw').write_bytes(FX.read_bytes())
try: R['D4_orphan_retry']=C.fixture_capture(d,5,datetime.now(timezone.utc))
except Exception as e: R['D4_orphan_retry']='UNHANDLED '+type(e).__name__
# D5 tail truncation undetected
d=fresh()
for s in (1,2,3): C.fixture_capture(d,s,datetime.now(timezone.utc))
L=d/'_test_store'/'ledger.jsonl'; L.write_bytes(b''.join(L.read_bytes().splitlines(True)[:1]))
try: R['D5_truncation_detected']=not C.SlotStore(d/'_test_store').validate()
except Exception: R['D5_truncation_detected']=True
# D6 watchdog flags in-progress current slot with no grace; grace param unused
d=fresh(); C.fixture_capture(d,1,datetime.now(timezone.utc)); R['D6_watchdog_current_slot']=C.watchdog(C.SlotStore(d/'_test_store'),2,datetime.now(timezone.utc),grace_seconds=3600)
# D7 row binds code/protocol hash?
r=json.loads((d/'_test_store'/'ledger.jsonl').read_text().splitlines()[0]); R['D7_row_fields']=sorted(r)
print(json.dumps(R,indent=1,default=str))
