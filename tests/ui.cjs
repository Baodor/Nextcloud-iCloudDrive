/* Run against the actual shipped UI with deterministic API fixtures. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const base = path.resolve(__dirname, '..');
const js = fs.readFileSync(path.join(base, 'nextcloud/icloud_drive/js/app.js'),'utf8');
const css = fs.readFileSync(path.join(base, 'nextcloud/icloud_drive/css/app.css'),'utf8');
const job = {id:'a'.repeat(32),name:'Documents',icloud_path:'Documents',nextcloud_path:'iCloud/Documents',mode:'bisync',schedule:'daily',time:'03:00',timezone:'Europe/Berlin',days:[0,1,2,3,4,5,6],interval_minutes:1440,enabled:true,backup:true,empty_dirs:true,conflict:'keep_both',initial:'icloud',max_delete_percent:10,transfers:2,checkers:4,retries:3,bandwidth:'',timeout_minutes:60,excludes:['.DS_Store','._*','~$*'],initialized:true,created:'2026-10-05T09:00:00Z',updated:'2026-10-05T09:00:00Z',next_run:'2026-10-06T01:00:00Z',last_success:'2026-10-05T09:10:00Z'};
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || (fs.existsSync(chromium.executablePath())?chromium.executablePath():undefined),args:['--no-sandbox']});
  try {
    for(const language of ['en','de']) {
      const context=await browser.newContext({viewport:{width:1440,height:960},timezoneId:'Europe/Berlin'});
      const page=await context.newPage();
      const errors=[];page.on('pageerror',e=>errors.push(e.message));
      const fixture={connection:{icloud_connected:true,nextcloud_connected:true,apple_id:'example@example.com',nextcloud_username:'member'},jobs:[{...job}],runs:[],nextcloud_user:'member'};
      let saved;
      await page.route('https://bridge.test/**',async route=>{
        const url=new URL(route.request().url());
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:`<!doctype html><html lang="${language}"><meta charset="utf-8"><style>body{margin:0;height:100vh}button{border:1px solid #e3e8ef;background:white} ${css}</style><div id="icloud-bridge" data-admin="true"></div><script>window.OC={linkToOCS:(x,v)=>'/ocs/v'+v+'.php/'+x+'/',requestToken:'test-token'}</script><script>${js}</script></html>`});
        const endpoint=url.pathname.split('/api/')[1];
        let data={};
        if(endpoint==='state')data=fixture;
        else if(endpoint==='admin')data={worker_url:'http://icloud-bridge:8080',token_configured:true};
        else if(endpoint==='folders')data={path:url.searchParams.get('path')||'',folders:[{name:'Documents',path:'Documents'}]};
        else if(endpoint?.startsWith('jobs/')&&route.request().method()==='PUT'){saved=route.request().postDataJSON().payload;Object.assign(fixture.jobs[0],saved);data=fixture.jobs[0];}
        else if(endpoint===`jobs/${job.id}/run`){
          const run={id:'b'.repeat(32),job_id:job.id,job_name:fixture.jobs[0].name,action:route.request().postDataJSON().payload.action,status:'running',started:'2026-10-05T10:00:00Z',finished:null,progress:{phase:'scanning',percent:null,direction:'nextcloud_to_icloud'},stats:{bytes:1024,totalBytes:1024,speed:0,listed:200,checks:50},log:''};
          fixture.runs=[run];data=run;
        }
        else if(endpoint===`runs/${'b'.repeat(32)}/stop`){
          assert.equal(route.request().method(),'POST');
          assert.deepEqual(route.request().postDataJSON(),{payload:{}});
          fixture.runs[0].cancel_requested=true;data=fixture.runs[0];
        }
        else if(endpoint==='dav')data={url:'http://icloud-bridge:8080/dav/test/',username:'test',password:'fixture-mount-password'};
        return route.fulfill({contentType:'application/json',body:JSON.stringify({ocs:{meta:{status:'ok'},data}})});
      });
      await page.goto('https://bridge.test/');
      await page.getByRole('heading',{name:language==='de'?'Übersicht':'Overview',exact:true}).waitFor();
      await page.getByRole('button',{name:language==='de'?'Einstellungen':'Settings',exact:true}).click();
      await page.locator('#job_name').fill('Work documents');
      await page.getByText(language==='de'?'Filter und Übertragungseinstellungen':'Filters and transfer settings',{exact:true}).click();
      await page.locator('#transfers').fill('3');
      await page.locator('#bandwidth').fill('10M');
      await page.getByRole('button',{name:language==='de'?'Ordner speichern':'Save folder',exact:true}).click();
      await page.waitForFunction(()=>!document.querySelector('dialog'));
      assert.equal(saved.transfers,3);assert.equal(saved.bandwidth,'10M');assert.equal(saved.backup,true);
      await page.getByRole('button',{name:language==='de'?'Verbindungen':'Connections',exact:true}).click();
      await page.getByRole('button',{name:language==='de'?'Zugangsdaten zur Einbindung anzeigen':'Show mount credentials'}).click();
      await page.locator('#dav_url').waitFor();assert.equal(await page.locator('#dav_password').inputValue(),'fixture-mount-password');
      await page.getByRole('button',{name:language==='de'?'Übersicht':'Overview',exact:true}).click();
      if(language==='en') {
        fs.mkdirSync(path.join(base,'docs/images'),{recursive:true});
        await page.screenshot({path:path.join(base,'docs/images/overview.png'),fullPage:true});
      }
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'mobile page should not scroll sideways');
      await page.getByRole('button',{name:language==='de'?/Ordner hinzufügen/:/Add folder/}).click();
      await page.locator('dialog[open]').waitFor();
      assert.equal(await page.evaluate(()=>document.querySelector('dialog').getBoundingClientRect().width<=window.innerWidth),true);
      await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('dialog'));
      await page.getByRole('button',{name:language==='de'?'Jetzt abgleichen':'Sync now',exact:true}).click();
      await page.locator('.ib-run-row').waitFor();
      const bar=page.locator('.ib-run-row .ib-run-progress > .ib-progress');
      assert.equal(await bar.getAttribute('aria-valuenow'),null,'a growing scan must not claim 100%');
      assert.equal(await page.locator('.ib-run-row .ib-badge').getAttribute('class'),'ib-badge blue');
      fixture.runs[0].progress={phase:'transferring',percent:25,totals_known:true,direction:'nextcloud_to_icloud'};
      fixture.runs[0].stats={bytes:1024,totalBytes:4096,speed:512,eta:6,transfers:0,totalTransfers:1,listed:200,checks:50,transferring:[{name:'<report>.txt',bytes:1024,size:4096,percentage:25}]};
      await page.getByText(language==='de'?'Bekannte Übertragungen: 25%':'Known transfers: 25%',{exact:true}).waitFor();
      assert.equal(await bar.getAttribute('aria-valuenow'),'25');
      assert.equal(await page.locator('.ib-transfer-name').textContent(),'<report>.txt');
      assert.equal(await page.locator('.ib-transfer .ib-progress').getAttribute('aria-valuenow'),'25');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'active file progress should fit mobile');
      if(process.env.UI_PROGRESS_SCREENSHOT&&language==='en')await page.screenshot({path:process.env.UI_PROGRESS_SCREENSHOT,fullPage:true});
      await page.getByRole('button',{name:'Details',exact:true}).click();
      await page.locator('dialog[data-run-id]').waitFor();
      fixture.runs[0].log='Fresh progress log\n'+Array.from({length:60},(_,i)=>`Line ${i}`).join('\n');
      fixture.runs[0].progress={phase:'scanning',percent:null,totals_known:false,direction:'icloud_to_nextcloud'};
      fixture.runs[0].stats.bytes=fixture.runs[0].stats.totalBytes;
      await page.locator('dialog').getByText('iCloud → Nextcloud',{exact:true}).waitFor();
      await page.waitForFunction(()=>document.querySelector('.ib-run-detail-log').textContent.startsWith('Fresh progress log'));
      await page.locator('.ib-run-detail-log').evaluate(log=>{log.scrollTop=0;});
      fixture.runs[0].log+='\nNew line while reading earlier output';
      await page.waitForFunction(()=>document.querySelector('.ib-run-detail-log').textContent.includes('New line while reading earlier output'));
      assert.equal(await page.locator('.ib-run-detail-log').evaluate(log=>log.scrollTop),0,'live log updates must preserve reading position');
      await page.getByRole('button',{name:language==='de'?'Schließen':'Close',exact:true}).last().click();
      assert.equal(await bar.getAttribute('aria-valuenow'),null,'the opposite direction must not retain an old percentage');
      assert.equal(await page.locator('.ib-run-row .ib-badge').getAttribute('class'),'ib-badge blue');
      await page.getByRole('button',{name:language==='de'?'Stoppen':'Stop',exact:true}).click();
      const stopRequested=page.getByRole('button',{name:language==='de'?'Stop angefordert':'Stop requested',exact:true});
      await stopRequested.waitFor();assert.equal(await stopRequested.isDisabled(),true);
      assert.equal(await page.locator('.ib-run-row .ib-badge').textContent(),language==='de'?'Wird gestoppt…':'Stopping…');
      fixture.runs[0].status='stopped';fixture.runs[0].finished='2026-10-05T10:01:00Z';
      await page.reload();
      await page.getByRole('button',{name:language==='de'?'Aktivität':'Activity',exact:true}).click();
      await page.locator('.ib-run-row').waitFor();
      assert.equal(await page.locator('.ib-run-row .ib-badge').textContent(),language==='de'?'Gestoppt':'Stopped');
      assert.equal(await page.locator('[data-action="stop"]').count(),0);
      fixture.runs[0].status='failed';fixture.runs[0].cancel_requested=false;fixture.runs[0].error='is a file not a directory';
      fixture.runs[0].progress={phase:'failed',percent:null,problem:{code:'file_directory_conflict',path:'Documents/Report.key',iwork_package:true}};
      fixture.runs[0].stats={bytes:0,totalBytes:0,checks:1735,totalChecks:1735,errors:235,listed:6048};
      await page.reload();await page.getByRole('button',{name:language==='de'?'Aktivität':'Activity',exact:true}).click();
      assert.equal(await page.locator('.ib-run-row .ib-badge').getAttribute('class'),'ib-badge bad');
      assert.equal(await bar.getAttribute('aria-valuenow'),null,'completed checks must not turn a failed run into 100%');
      await page.getByText('Documents/Report.key',{exact:true}).waitFor();
      fixture.runs[0].status='success';fixture.runs[0].action='preview';delete fixture.runs[0].error;
      fixture.runs[0].progress={phase:'success',percent:100};fixture.runs[0].stats={bytes:4096,totalBytes:4096,transfers:2,totalTransfers:2,speed:0};
      await page.reload();await page.getByRole('button',{name:language==='de'?'Aktivität':'Activity',exact:true}).click();
      assert.equal(await bar.getAttribute('aria-valuenow'),'100');
      assert.equal(await page.locator('.ib-run-row .ib-badge').textContent(),language==='de'?'Vorschau abgeschlossen':'Preview complete');
      assert.equal(await page.locator('.ib-progress-stats').textContent().then(text=>text.includes('/s')),false,'preview statistics must not claim network throughput');
      assert.deepEqual(errors,[]);
      await context.close();
      console.log(`UI checks passed (${language}, desktop and mobile)`);
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
