// Runs config/50-ollama1.rules against a fake polkit and checks what it allows.
// node tests/polkit_check.js config/50-ollama1.rules
const fs=require('fs');let rule;const polkit={Result:{YES:'yes',NO:'no',NOT_HANDLED:undefined},addRule:f=>rule=f};
eval(fs.readFileSync(process.argv[2],'utf8'));
const t=(user,id,unit,verb)=>rule({id,lookup:k=>({unit,verb})[k]},{user});
const cases=[['o1admin','org.freedesktop.systemd1.manage-units','ollama1-restart.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-restart.service','stop','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ssh.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-pull@0123456789ab.service','start','yes'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-pull@0123456789abc.service','start','no'],
['o1admin','org.freedesktop.systemd1.manage-units','ollama1-rmdevice@0123456789abcdef.service','start','yes'],
['o1admin','org.freedesktop.login1.reboot',undefined,undefined,'no'],
['pmiller','org.freedesktop.systemd1.manage-units','ollama1-restart.service','start',undefined]];
let bad=0;for(const c of cases){const r=t(...c.slice(0,4));if(r!==c[4]){bad++;console.log('MISMATCH',c,r);}}
console.log(bad?'FAIL':'polkit rule ok ('+cases.length+' cases)');
