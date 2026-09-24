import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('usage',Path(__file__).resolve().parents[1]/'plugins/codex-daily-usage/scripts/codex_daily_usage.py')
usage=importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)

class UsageChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def state(self,data):
        (self.root/'.codex-global-state.json').write_text(json.dumps(data),encoding='utf-8')
    def log(self,cid,kind='task'):
        folder=self.root/'sessions'; folder.mkdir(exist_ok=True)
        source={'subagent':{'other':'guardian'}} if kind=='guardian' else 'cli'
        records=[{'type':'session_meta','payload':{'session_id':cid,'source':source,'cwd':'/tmp/random'}},
            {'type':'turn_context','payload':{'model':'model-a'}},
            {'type':'response_item','payload':{'role':'user','type':'message','content':[{'type':'input_text','text':'=a task <script>'}]}}]
        for n,(total,last) in enumerate([(100,100),(100,100),(200,100),(50,50)]):
            records.append({'type':'event_msg','timestamp':f'2026-09-24T00:00:0{n}+08:00','payload':{'type':'token_count','info':{'total_token_usage':{'total_tokens':total},'last_token_usage':{'total_tokens':last}},'rate_limits':{'secondary':{'used_percent':10,'window_minutes':10080}}}})
        path=folder/(cid+'.jsonl');path.write_text('\n'.join(map(json.dumps,records)),encoding='utf-8');return path
    def test_selected_and_single_host_discovery(self):
        self.state({'codex-managed-remote-connections':[{'hostId':'h1','alias':'server1'},{'hostId':'h2','alias':'server2'}],'selected-remote-host-id':'h2'})
        self.assertEqual(usage.discover_ssh_hosts(self.root),(['server1','server2'],'server2'))
        self.state({'remote-projects':[{'hostId':'remote-ssh-discovered:only'}]})
        self.assertEqual(usage.discover_ssh_hosts(self.root),(['only'],'only'))
    def test_ambiguous_sources_not_silently_local(self):
        self.state({'remote-projects':[{'hostId':'remote-ssh-discovered:a'},{'hostId':'remote-ssh-discovered:b'}]})
        with patch.object(usage,'_local_codex_home',return_value=self.root),patch.object(sys,'argv',['usage']),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:usage.parse_args()
            self.assertEqual(error.exception.code,2)
    def test_unchanged_cumulative_and_reset(self):
        result=usage.collect_usage([self.log('task-1')])
        self.assertEqual(sum(result[0].values()),3)
        self.assertEqual(sum(result[1].values()),250)
        self.assertEqual(len(result[8]),3)
        self.assertEqual({cid for _,cid in result[1]}, {'task-1'})
    def test_registered_remote_project_name_and_short_fallback(self):
        db=sqlite3.connect(self.root/'state_5.sqlite')
        db.execute('create table threads (id text, name text, title text, cwd text)')
        db.executemany('insert into threads values (?,?,?,?)',[('one','真实名称','prompt','/tmp/random'),('two',None,'x'*300,'/tmp/random')]);db.commit();db.close()
        self.state({'remote-projects':[{'id':'p','label':'蛋白项目'}],'thread-project-assignments':{'one':{'projectId':'p'}},'projectless-thread-ids':['two']})
        labels=usage._apply_thread_metadata(self.root,{'one':'old','two':'badproject · old'}, {})
        self.assertEqual(labels['one'],'蛋白项目 · 真实名称')
        self.assertLessEqual(len(labels['two']),80)
        self.assertNotIn('badproject',labels['two'])
    def test_cross_source_deduplication(self):
        self.log('task-1')
        args=usage.argparse.Namespace(codex_home=self.root,from_date='2026-09-24',to_date='2026-09-24',days=30)
        source=usage.local_summary(args)
        with patch.object(usage,'_local_codex_home',return_value=self.root): merged=usage.merged_summary([source,source])
        self.assertEqual(sum(x['tokens'] for x in merged['conversation_tokens']),250)
        self.assertEqual(merged['cross_source_duplicates'],3)
    def test_real_cli_outputs_exclude_guardian_from_user_denominator(self):
        self.log('user')
        # Different timestamps ensure these are different calls, not copied logs.
        guardian=self.log('guardian','guardian');guardian.write_text(guardian.read_text().replace('00:00:', '01:00:'),encoding='utf-8')
        output=self.root/'report.html';summary=self.root/'summary.json'
        argv=['usage','--local','--codex-home',str(self.root),'--from-date','2026-09-24','--to-date','2026-09-24','--output',str(output),'--summary-output',str(summary)]
        with patch.object(usage,'_local_codex_home',return_value=self.root),patch.object(sys,'argv',argv),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(usage.main(),0)
        data=json.loads(summary.read_text(encoding='utf-8'))
        self.assertEqual(data['user_weekly_tokens'],250)
        self.assertEqual(data['background_weekly_tokens'],250)
        self.assertIn('&lt;script&gt;',output.read_text(encoding='utf-8'))
        self.assertNotIn('<script>',output.read_text(encoding='utf-8'))
        self.assertIn("'=a task",output.with_suffix('.csv').read_text(encoding='utf-8-sig'))
    def test_failed_remote_report_is_explicitly_partial(self):
        self.log('user')
        self.state({'remote-projects':[{'hostId':'remote-ssh-discovered:unavailable'}]})
        output=self.root/'partial.html'
        argv=['usage','--all-sources','--from-date','2026-09-24','--to-date','2026-09-24','--output',str(output)]
        with patch.object(usage,'_local_codex_home',return_value=self.root),patch.object(sys,'argv',argv),patch.object(usage,'_remote_usage_summary',side_effect=RuntimeError('offline')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(usage.main(),3)
        self.assertIn('unavailable：读取失败，未计入',output.read_text(encoding='utf-8'))

    def test_guardian_shared_session_id_does_not_overwrite_parent(self):
        parent=self.log('parent')
        child=self.log('child','guardian')
        for file,cid in [(parent,'parent'),(child,'child')]:
            records=[json.loads(line) for line in file.read_text().splitlines()]
            records[0]['payload']['id']=cid
            records[0]['payload']['session_id']='parent'
            if cid=='child':
                for record in records:
                    if 'timestamp' in record:record['timestamp']=record['timestamp'].replace('00:00:','01:00:')
            file.write_text('\n'.join(map(json.dumps,records)),encoding='utf-8')
        for paths in ([parent,child],[child,parent]):
            parsed=usage.collect_usage(paths)
            self.assertEqual(parsed[9],{'parent':'task','child':'guardian'})
            totals={cid:sum(n for (_,ident),n in parsed[1].items() if ident==cid) for cid in ('parent','child')}
            self.assertEqual(totals,{'parent':250,'child':250})

    def test_fork_embedded_metadata_cannot_change_owner(self):
        path=self.log('child')
        records=[json.loads(line) for line in path.read_text().splitlines()]
        records[0]['payload'].update({'id':'child','session_id':'parent','timestamp':'2026-09-24T00:00:02+08:00'})
        records.insert(1,{'type':'session_meta','payload':{'id':'parent','session_id':'parent','source':'cli','timestamp':'2026-09-23T00:00:00+08:00'}})
        path.write_text('\n'.join(map(json.dumps,records)),encoding='utf-8')
        parsed=usage.collect_usage([path])
        self.assertEqual({cid for _,cid in parsed[1]}, {'child'})
        self.assertEqual(sum(parsed[1].values()),150)
        self.assertEqual(len(parsed[8]),2)

if __name__=='__main__':unittest.main(verbosity=2)
