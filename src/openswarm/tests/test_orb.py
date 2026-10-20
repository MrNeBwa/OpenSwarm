"""The activity sphere: state machine + renderer, executed in node against a validating canvas stub."""
import json, shutil, subprocess
from pathlib import Path

from .helpers import Env

HTML = Path(__file__).resolve().parent.parent / "web_ui.html"

HARNESS = r"""
const assert=require('assert');
eval(MODULE+";globalThis.O={orbInit,orbReduce,orbCounts,orbSummary,orbEnergy,orbHue,orbFmtTime,orbLang,OrbRenderer};");
const O=globalThis.O;const out={};
// ---- reducer lifecycle
let s=O.orbInit("chat");assert.equal(s.phase,"idle");
O.orbReduce(s,{type:"user",mode:"storm",t:100});assert.equal(s.phase,"thinking");assert.equal(s.mode,"storm");
O.orbReduce(s,{type:"agent",agent:"a",title:"Worker A",role:"coder"});O.orbReduce(s,{type:"agent",agent:"b",title:"Worker B",role:"coder"});
O.orbReduce(s,{type:"status",agent:"a",status:"Running"});assert.equal(s.phase,"working");
let c=O.orbCounts(s);assert.deepEqual([c.running,c.queued,c.done,c.failed],[1,1,0,0]);
O.orbReduce(s,{type:"tool",tool:"bash",status:"running",summary:"ls -la"});assert.equal(s.tool.name,"bash");
O.orbReduce(s,{type:"tool",tool:"bash",status:"completed"});assert.equal(s.tool,null);
O.orbReduce(s,{type:"permission"});assert.equal(s.phase,"waiting");assert.equal(O.orbHue(s),42);
O.orbReduce(s,{type:"permission_resolved"});assert.equal(s.phase,"working");
O.orbReduce(s,{type:"usage",agent:"a",tokens:{input:120,output:45},cost:0.0123});
O.orbReduce(s,{type:"status",agent:"a",status:"Done"});O.orbReduce(s,{type:"status",agent:"b",status:"Failed"});
const sm=O.orbSummary(s,(100+65)*1000,"en","⚡ Storm");
assert.equal(sm.phase,"Working");assert.ok(sm.detail.includes("1 done")&&sm.detail.includes("1 failed"),sm.detail);
assert.ok(sm.time.includes("1:05")&&sm.time.includes("120→45")&&sm.time.includes("$0.0123"),sm.time);
const ru=O.orbSummary(s,(100+5)*1000,"ru","x");assert.equal(ru.phase,"Работает");assert.ok(ru.detail.includes("готово"));
O.orbReduce(s,{type:"done",ok:true,t:170});assert.equal(s.phase,"done");assert.equal(s.flash,"ok");
assert.equal(O.orbSummary(s,999999999,"en","m").time.split("·")[0].trim(),"Elapsed 1:10");  // frozen at the end time, not 'now'
O.orbReduce(s,{type:"user",mode:"chat",t:300});assert.deepEqual(s.agents,{});assert.equal(s.phase,"thinking");   // new run resets
O.orbReduce(s,{type:"done",ok:false,t:301});assert.equal(s.phase,"error");assert.equal(O.orbHue(s),356);
// a running agent is closed when the run ends
const s2=O.orbInit("research");O.orbReduce(s2,{type:"user",t:1});O.orbReduce(s2,{type:"agent",agent:"x",title:"X"});O.orbReduce(s2,{type:"status",agent:"x",status:"Running"});O.orbReduce(s2,{type:"done",ok:false,t:2});
assert.equal(s2.agents.x.status,"Failed");
assert.equal(O.orbFmtTime(3599),"59:59");assert.equal(O.orbHue({mode:"wf:article",phase:"idle"}),290);assert.equal(O.orbHue({mode:"nope",phase:"idle"}),215);
// ---- renderer against a validating canvas stub
const COL=/^hsla\(-?\d+(\.\d+)?,\d+(\.\d+)?%,\d+(\.\d+)?%,(0|1|0?\.\d+|\d\.\d+)\)$/;
function stub(){let calls=0;const st={};const ctx=new Proxy({},{get(_,k){
  if(k==="__calls")return calls;if(k in st)return st[k];
  if(k==="createRadialGradient")return(...a)=>{calls++;a.forEach(x=>assert.ok(Number.isFinite(x),"gradient arg "+x));return{addColorStop(o,c){assert.ok(COL.test(c),"bad colour "+c);}};};
  return(...a)=>{calls++;a.forEach(x=>{if(typeof x==="number")assert.ok(Number.isFinite(x),k+" got "+x);});};},
  set(_,k,v){if((k==="fillStyle"||k==="strokeStyle")&&typeof v==="string")assert.ok(COL.test(v),"bad colour "+v);if(k==="lineWidth")assert.ok(Number.isFinite(v)&&v>0);st[k]=v;return true;}});
  return{canvas:{width:0,height:0,clientWidth:240,getContext:()=>ctx},ctx};}
const modes=["chat","auto","storm","best","cowork","research","write","review","wf:x"];
const phases=["idle","thinking","working","waiting","done","error"];let maxCalls=0,frames=0;
for(const m of modes)for(const ph of phases)for(const n of [0,1,30])for(const reduce of [false,true]){
  const {canvas,ctx}=stub();const st=O.orbInit(m);st.phase=ph;st.pulses=3;if(ph==="done"||ph==="error"){st.flash=ph==="done"?"ok":"bad";st.flashAt=5;}
  for(let i=0;i<n;i++)st.agents["a"+i]={title:"A"+i,role:"coder",status:["Running","Done","Failed","Idle","Skipped","Merging","weird"][i%7]};
  const R=new O.OrbRenderer(canvas,()=>st,{reduceMotion:reduce});R.resize();
  for(let f=0;f<45;f++){const before=ctx.__calls;R.render(f*33);if(f===44){maxCalls=Math.max(maxCalls,ctx.__calls-before);}frames++;}
  if(ph==="working"&&!reduce&&n===1){st.pulses=9;R.render(2000);assert.ok(R.rings.length>=1,"pulse ring expected in "+m+" rings="+R.rings.length+" seen="+R.pulseSeen+" pulses="+st.pulses);}
}
out.frames=frames;out.maxCallsPerFrame=maxCalls;assert.ok(maxCalls<3000,"too many canvas calls: "+maxCalls);
// energy follows phase smoothly; flash decays
const {canvas}=stub();const st=O.orbInit("chat");const R=new O.OrbRenderer(canvas,()=>st,{});R.resize();
for(let f=0;f<10;f++)R.render(f*33);const idleE=R.energy;st.phase="working";st.agents.a={title:"a",status:"Running"};for(let f=10;f<120;f++)R.render(f*33);
assert.ok(idleE<.2&&R.energy>.6,"energy "+idleE+" -> "+R.energy);
st.phase="done";st.flash="ok";st.flashAt=77;R.render(5000);const fl=R.flashLevel;for(let f=0;f<80;f++)R.render(5000+f*33);assert.ok(fl>.9&&R.flashLevel<.05,"flash "+fl+" -> "+R.flashLevel);
out.energy=[idleE,R.energy];console.log(JSON.stringify(out));
"""


class Orb(Env):
    def test_state_machine_and_renderer(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = HTML.read_text(encoding="utf-8")
        module = html.split("/*ORB:BEGIN*/")[1].split("/*ORB:END*/")[0]
        script = self.tmp / "orb_test.js"
        script.write_text("const MODULE=" + json.dumps(module) + ";\n" + HARNESS)
        r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertGreater(out["frames"], 2000)
