"""Validate cold and warm startup work against elapsed monotonic events and the exact script."""
import datetime as dt
import json
import math


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line]


def only(rows, predicate, label):
    matches=[item for item in rows if predicate(item)]
    if len(matches) != 1: raise ValueError(f"Expected one {label}, got {len(matches)}")
    return matches[0]


def decode(row, data, mode, scenario, ui, *, complete_series=True):
    cold_start = ui["second_down_qpc"] * 10**9 // ui["frequency"]
    outputs=[]
    stages = {key:round((ui[key]-ui["second_down_qpc"])*1000/ui["frequency"],3)
              for key in ("process_observed_qpc","window_qpc","ready_seen_qpc","nav_qpc","selected_qpc")}
    for run in ui["runs"]:
        index=run['index']
        xid = run["execution_id"]
        history=lines(data/"history"/(xid+".jsonl"))
        messages=[x for x in history if x["execution_id"] == xid and x["event"]=="script_log"]
        first=only(messages,lambda x: x["message"]=="ST-040/first","Lua first log")
        prefix="ST-040/result:" if scenario["kind"]=="lua" else "ST-040/ocr:"
        log=only(messages,lambda x:x["message"]==prefix+str(scenario["expected"]),"correct result")
        outcome=dict(execution_id=xid,script_id=run["script_id"],text=log["message"][len(prefix):],ui_stages_ms=stages)
        assert run["script_id"] == scenario["script_id"] and run["status"] == "completed"
        if mode=="baseline":
            anchor=dt.datetime.fromisoformat(ui["qpc_utc_anchor"])
            def delta(event): return round((dt.datetime.fromisoformat(event["timestamp"])-anchor).total_seconds()*1000,3)
            end_log = first if scenario['kind']=='lua' else log
            frequency = ui['frequency']
            # The legacy journal truncates UTC to milliseconds. Bracket the true
            # offset with both physical QPC anchor reads instead of assuming sync.
            lower = delta(end_log)-(ui['second_down_qpc']-ui['anchor_before_qpc'])*1000/frequency
            upper = delta(end_log)+1-(ui['second_down_qpc']-ui['anchor_after_qpc'])*1000/frequency
            outcome.update(first_ms=delta(first),result_ms=delta(log),
                           duration_ms=(lower+upper)/2,legacy_interval_ms=[lower,upper],
                           ui_terminal_upper_ms=(run['terminal_seen_qpc']-ui['second_down_qpc'])*1000/frequency,
                           clock="RC06 physical second-down wall anchor to actual Lua log wall timestamp (millisecond resolution)")
        else:
            marks=lines(data/"diagnostics/startup-timing.jsonl")
            events=[a for a in marks if a.get("execution_id")==xid]
            def event(name): return only(events,lambda a:a["event"]==name,name)
            start=event("execute_accepted")
            first_mark=event("lua_first_statement")
            done=event("script_finished")
            assert all(x.get('script_id')==scenario['script_id'] for x in (start,first_mark,done))
            assert first_mark["session_id"]==start["session_id"]==done["session_id"] and done['status']=='completed'
            assert done["result"]==scenario["expected"], done
            session=[a for a in marks if a.get("session_id")==start["session_id"]]
            ready=only(session,lambda a:a["event"]=="system_ready","first system_ready")
            sidecar=only(session,lambda a:a['event']=='sidecar_entry','sidecar entry')
            assert cold_start<=sidecar['qpc_ns']<=ready["qpc_ns"]<=start["qpc_ns"]<=first_mark["qpc_ns"]<=done['qpc_ns']
            assert run['invoke_qpc']*10**9//ui['frequency']<=start['qpc_ns']
            if scenario["kind"]=="lua":
                assert not any(x['event'].startswith('ocr_worker_') for x in session)
                endpoint=first_mark
                hot_start=start
            else:
                sent=event("ocr_submitted")
                request_marks=[x for x in events if x['event']=='lua_ocr_request']
                # Development pilots can expose the older internal boundary. The
                # formal gate requires the actual Lua OCR call before screen capture.
                if mode=='pilot' and not request_marks:
                    request=sent
                    outcome['missing_lua_request']=True
                else:
                    request=event('lua_ocr_request')
                    assert request['operation_id']==sent['operation_id']
                result=event("ocr_result")
                returned=event("lua_ocr_return")
                assert sent["operation_id"] and sent["operation_id"]==result["operation_id"]
                assert returned["actual_text"] == scenario["expected"]
                infer=event('ocr_inference')
                cache=event('ocr_cache')
                assert cache['hit'] is False and infer['worker_pid']>0
                if index:
                    assert run['warm_worker']['pid']==infer['worker_pid'] and run['warm_worker']['qpc']<=run['invoke_qpc']
                    assert not any(x['event'].startswith('ocr_worker_') for x in events)
                else:
                    opened=event('ocr_worker_started')
                    loaded=event('ocr_worker_ready')
                    assert opened['worker_pid']==loaded['worker_pid']==infer['worker_pid']
                    assert sent['qpc_ns']<=opened['qpc_ns']<=loaded['qpc_ns']<=infer['qpc_ns']
                assert sent['operation_id']==infer['operation_id']==cache['operation_id']==returned['operation_id']
                assert sent.get('job_id') is None
                assert first_mark["qpc_ns"]<=request['qpc_ns']<=sent["qpc_ns"]<=result["qpc_ns"]<=returned["qpc_ns"]<=done["qpc_ns"]
                assert sent["model_id"]==scenario["model_id"]
                endpoint,hot_start=returned,request
                outcome.update(operation_id=sent["operation_id"],model_id=sent["model_id"],worker_pid=infer['worker_pid'],
                               submit_to_lua_ms=(returned['qpc_ns']-sent['qpc_ns'])/1e6,
                               request_to_lua_ms=None if outcome.get('missing_lua_request') else (returned['qpc_ns']-request['qpc_ns'])/1e6)
            outcome.update(session_id=start["session_id"], first_ms=(first_mark["qpc_ns"]-cold_start)/1e6,
                           duration_ms=(endpoint["qpc_ns"]-(hot_start["qpc_ns"] if index else cold_start))/1e6,
                           endpoint_qpc_ns=endpoint['qpc_ns'],sidecar_qpc_ns=sidecar['qpc_ns'],ready_qpc_ns=ready['qpc_ns'],
                           terminal_poll_lag_ms=(run['terminal_seen_qpc']*10**9//ui['frequency']-done['qpc_ns'])/1e6,
                           accepted_observation_lag_ms=(run['accepted_seen_qpc']*10**9//ui['frequency']-start['qpc_ns'])/1e6,
                           events=events,clock="QPC nanoseconds")
        outputs.append(outcome)
    if complete_series and mode in ('warm','pilot') and scenario['kind']!='lua':
        assert len({r['operation_id'] for r in outputs})==len(outputs)
        assert len({r['worker_pid'] for r in outputs})==1, 'Expected one retained worker for the warm pipeline'
        shown=[json.loads((row/'fixture'/f'shown-{i}.json').read_bytes())['rgb_sha256'] for i in range(len(outputs))]
        assert shown==[item['rgb_sha256'] for item in scenario['images'][:len(shown)]] and len(set(shown))==len(shown)
    return outputs


def summarize(rows,mode,kinds):
    groups={}
    for kind in kinds:
        if mode in ('baseline','pilot'):
            group=[row for row in rows if row['kind']==kind]
            groups[kind]=dict(passed=len(group)==1 and group[0]['correct'],
                              count=len(group),durations_ms=[x['duration_ms'] for row in group for x in row.get('executions',[])])
            continue
        samples=[]
        for row in rows:
            if row["kind"] !=kind: continue
            if row["correct"] or mode=='warm' and row.get('executions'):
                items=row["executions"][1:] if mode=="warm" else row["executions"]
                samples.extend(x["duration_ms"] for x in items)
            elif mode=='warm':
                samples.extend(x['duration_ms'] if x['duration_ms'] is not None else math.inf for x in row.get('warm_attempts',[]))
            else:
                samples.append(math.inf)
        samples.sort()
        def pct(fraction):
            if not samples:return None
            n=samples[math.ceil(len(samples)*fraction)-1]
            return "Infinity" if math.isinf(n) else n
        limit=3000 if kind=="lua" else 10000
        failures=sum(math.isinf(x) for x in samples)
        groups[kind]=dict(count=len(samples),not_run=20-len(samples),failures=failures,p50_ms=pct(.5),p95_ms=pct(.95),max_ms=pct(1),
                          limit_ms=limit if mode=="cold" else None,
                          passed=len(samples)==20 and not failures and all(r['correct'] for r in rows if r['kind']==kind)
                          and (mode!="cold" or pct(.95)<=limit))
    return dict(mode=mode,groups=groups,passed=all(g["passed"] for g in groups.values()))
