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
      assert.deepEqual(errors,[]);
      await context.close();
      console.log(`UI checks passed (${language}, desktop and mobile)`);
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
