// Runs config/50-ollama1.rules against a fake polkit and checks what it allows.
// node tests/polkit_check.js config/50-ollama1.rules
// Like polkit, every rule runs in order until one answers (NOT_HANDLED goes on to the next).
const fs=require('fs');const rules=[];const polkit={Result:{YES:'yes',NO:'no',NOT_HANDLED:undefined},addRule:f=>rules.push(f)};
eval(fs.readFileSync(process.argv[2],'utf8'));
const t=(user,id,unit,verb)=>{for(const rule of rules){const r=rule({id,lookup:k=>({unit,verb})[k]},{user});if(r!==undefined&&r!==null)return r;}return undefined;};
const cases=[['o1admin','org.freedesktop.systemd1.manage-units','ollama1-restart.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-restart.service','stop','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ssh.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-pull@0123456789ab.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-pull@0123456789abc.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-rmdevice@0123456789abcdef.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-models-sync.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-models-sync.service','stop','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-models-preview.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-power-apply.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-power.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@on-30.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@off-1440.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@on-30.service','stop','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@on-30000.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@on-.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@maybe-30.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@on-30;id.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@.service','start','no'],
['o1admin','org.freedesktop.login1.reboot',undefined,undefined,'no'],
// a model set from the app (6b410): the gateway's user starts that one unit, and nothing else
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','start','yes'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','stop','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','restart','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','reload-or-restart','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan@x.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service.d','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-restart.service','start','no'],
['o1dash','org.freedesktop.systemd1.manage-units','ollama1-quickburn.service','start','yes'],
['o1dash','org.freedesktop.systemd1.manage-units','ollama1-quickburn.service','stop','no'],
['o1dash','org.freedesktop.systemd1.manage-units','ollama1-quickburn.service','restart','no'],
['o1dash','org.freedesktop.systemd1.manage-units','ollama1-restart.service','start','no'],
['o1dash','org.freedesktop.systemd1.manage-units','ollama1-reboot.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-quickburn.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-quickburn.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-models-sync.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-pull@0123456789ab.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-units','ollama1-sleepcfg@on-30.service','start','no'],
['o1gw','org.freedesktop.systemd1.manage-unit-files','ollama1-modelplan.service','start','no'],
['o1gw','org.freedesktop.login1.reboot',undefined,undefined,'no'],
['alice','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','start',undefined],
['alice','org.freedesktop.systemd1.manage-units','ollama1-restart.service','start',undefined]];
let bad=0;for(const c of cases){const r=t(...c.slice(0,4));if(r!==c[4]){bad++;console.log('MISMATCH',c,r);}}
console.log(bad?'FAIL':'polkit rule ok ('+cases.length+' cases)');
