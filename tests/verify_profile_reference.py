import base64,contextlib,csv,io,json,sys,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch,Mock
import verify_usage as base
usage=base.usage

class ProfileReferenceChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.scope={'account':'account-hash','user':'user-hash'}
    def tearDown(self):self.tmp.cleanup()
    def auth(self):
        claims={'sub':'user','https://api.openai.com/auth':{'chatgpt_account_id':'account','chatgpt_user_id':'user'}}
        part=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        (self.root/'auth.json').write_text(json.dumps({'tokens':{'access_token':'PRIVATE_ACCESS','refresh_token':'PRIVATE_REFRESH','id_token':'x.'+part+'.z','account_id':'account'}}),encoding='utf-8')
    def ref(self,days=None):
        return {'status':'ok','source':'Profile 兼容统计接口','timezone':'UTC','fetched_at':'2026-09-24T06:00:00+00:00','days':days or {f'2026-09-{d}':1000 for d in range(18,25)},'_scope':self.scope}
    def test_reference_fetch_fixed_origin_and_no_credentials_in_output(self):
        self.auth();response=io.BytesIO(json.dumps({'stats':{'daily_usage_buckets':[{'start_date':'2026-09-23','tokens':123450}]},'metadata':{}}).encode())
        opener=Mock();opener.open.return_value=response
        with patch.object(usage.urllib.request,'build_opener',return_value=opener):result=usage.fetch_profile_reference(self.root)
        self.assertEqual(result['days']['2026-09-23'],123450)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://chatgpt.com/backend-api/wham/profiles/me')
        self.assertEqual(request.get_method(),'GET')
        text=json.dumps(result)
        self.assertNotIn('PRIVATE_ACCESS',text);self.assertNotIn('PRIVATE_REFRESH',text)
        self.assertEqual(usage.NoProfileRedirect().redirect_request(None,None,302,'',{},'https://example.org'),None)
    def test_http_denial_is_not_zero_or_stale_data(self):
        self.auth();opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://chatgpt.com',403,'PRIVATE_ACCESS',{},None)
        with patch.object(usage.urllib.request,'build_opener',return_value=opener):result=usage.fetch_profile_reference(self.root)
        self.assertEqual(result['status'],'unavailable');self.assertEqual(result['days'],{})
        self.assertIn('403',result['error']);self.assertNotIn('PRIVATE_ACCESS',json.dumps(result))
    def test_missing_date_is_not_assumed_zero(self):
        date=usage.dt.date(2026,9,24)
        result=usage.profile_reference_for_period(self.ref({'2026-09-23':500}),date-usage.dt.timedelta(days=1),date,[{'account_scope':self.scope}])
        self.assertIsNone(result['period_total']);self.assertIsNone(result['week_total'])
        self.assertEqual(result['period_reported_total'],500);self.assertEqual(result['missing_dates'],['2026-09-24'])
        html=usage.profile_reference_html(result,[],date-usage.dt.timedelta(days=1),date)
        self.assertIn('未返回',html);self.assertIn('未按零计入',html)
        empty=usage.profile_reference_for_period(self.ref({'2026-01-01':1}),date,date,[{'account_scope':self.scope}])
        empty_html=usage.profile_reference_html(empty,[],date,date)
        self.assertIn('尚未返回',empty_html);self.assertNotIn('<strong>0</strong>',empty_html)
    def test_mixed_accounts_do_not_get_account_shares(self):
        day=usage.dt.date(2026,9,24)
        ref=usage.profile_reference_for_period(self.ref(),day,day,[{'account_scope':{'account':'other','user':'other'}}])
        self.assertFalse(ref['scope_verified'])
        page=usage.profile_reference_html(ref,[],day,day)
        self.assertIn('未核实来源账户',page)
    def test_profile_mode_sets_utc_and_all_sources(self):
        with patch.object(sys,'argv',['usage','--profile-reference']):args=usage.parse_args()
        self.assertTrue(args.all_sources);self.assertTrue(args.account_scope);self.assertEqual(args.utc_offset,'+0000')
        with patch.object(sys,'argv',['usage','--profile-reference','--utc-offset=+0800']),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):usage.parse_args()
    def test_reference_drives_display_and_weekly_percentages(self):
        for cid,kind in [('user','task'),('guardian','guardian')]:
            file=base.UsageChecks.log(self,cid,kind)
            text=file.read_text(encoding='utf-8').replace('+08:00','+00:00')
            if cid=='guardian':text=text.replace('00:00:','01:00:')
            file.write_text(text,encoding='utf-8')
        output=self.root/'report.html';summary=self.root/'summary.json'
        argv=['usage','--local','--profile-reference','--codex-home',str(self.root),'--from-date','2026-09-24','--to-date','2026-09-24','--output',str(output),'--summary-output',str(summary)]
        with patch.object(sys,'argv',argv),patch.object(usage,'_local_codex_home',return_value=self.root),patch.object(usage,'account_scope',return_value=self.scope),patch.object(usage,'fetch_profile_reference',return_value=self.ref()),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(usage.main(),0)
        data=json.loads(summary.read_text(encoding='utf-8'))
        self.assertEqual(data['display_total_tokens'],1000);self.assertEqual(data['period_totals']['total_tokens'],500)
        self.assertEqual(data['profile_reference']['week_total'],7000)
        with output.with_suffix('.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        self.assertEqual(sum(int(r['近7天令牌数']) for r in rows),7000)
        self.assertEqual(int(rows[-1]['近7天令牌数']),6500)
        with output.with_suffix('.daily.csv').open(encoding='utf-8-sig',newline='') as f:daily_rows=list(csv.DictReader(f))
        self.assertEqual(sum(int(r['记录tokens或对账差额']) for r in daily_rows),1000)
        gap_row=next(row for row in daily_rows if row['类别']=='对账差额')
        self.assertEqual(int(gap_row['记录tokens或对账差额']),500)
        daily_values=[int(row['记录tokens或对账差额']) for row in daily_rows]
        self.assertEqual(daily_values,[500,250,250])
        self.assertAlmostEqual(float(rows[0]['占账户近7天tokens比例'].rstrip('%')),250/7000*100,places=5)
        page=output.read_text(encoding='utf-8')
        self.assertIn('账户 token 总量（兼容接口）',page);self.assertIn('尚未归属的对账差额',page)

if __name__=='__main__':unittest.main(verbosity=2)
