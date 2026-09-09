"""Automatic cleanup must free downloads only after all scans release their inputs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.config import settings
from app.core import db
from app.core.download_cleanup import cleanup_downloads, download_session, managed_download_session
from app.core.models import TaskStatus
from app.engine.pipeline import ScanPipeline, ScanResult


class DownloadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pailei-cleanup-')
        self.root = Path(self.temp.name)
        self.patches = [patch.object(settings,'db_path', self.root/'app.db'),
                        patch.object(settings,'files_dir', self.root/'files'),
                        patch.object(settings,'cache_dir', self.root/'cache'),
                        patch.object(settings,'reports_dir', self.root/'reports')]
        for p in self.patches: p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def write(self, path, body=b'file', days_ago=0):
        path=self.root/path;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        if days_ago:
            stamp=(datetime.now().astimezone()-timedelta(days=days_ago)).timestamp()
            os.utime(path,(stamp,stamp))
        return path

    def test_today_downloads_caches_and_part_removed(self):
        files=[self.write(p) for p in ['files/cninfo/a.pdf','files/hkexnews/b.pdf',
                                      'files/_blob/abc.pdf','files/cninfo/c.pdf.part','cache/pdf_parse/a.json']]
        result=cleanup_downloads()
        self.assertEqual(result['removed_files'],5)
        self.assertTrue(all(not f.exists() for f in files))
        self.assertEqual(result['status'],'completed')

    def test_reports_financial_raw_manifest_and_model_data_preserved(self):
        keep=[self.write(p) for p in ['reports/a.html','reports/a.json','raw/eastmoney/a.json',
                                     'source_manifest/a.json','cache/llm/a.json','files/readme.txt']]
        cleanup_downloads()
        self.assertTrue(all(f.exists() for f in keep))
        self.assertTrue(settings.db_path.exists())

    def test_previous_day_not_deleted_without_pending_marker(self):
        pdf=self.write('files/cninfo/old.pdf',days_ago=1)
        cache=self.write('cache/pdf_parse/old.json',days_ago=1)
        cleanup_downloads()
        self.assertTrue(pdf.exists());self.assertTrue(cache.exists())

    def test_pending_previous_day_is_retried(self):
        pdf=self.write('files/cninfo/old.pdf',days_ago=1)
        yesterday=(datetime.now().astimezone()-timedelta(days=1)).date().isoformat()
        marker=self.write(f'download_cleanup/{yesterday}.pending')
        result=cleanup_downloads()
        self.assertFalse(pdf.exists());self.assertFalse(marker.exists())
        self.assertIn(yesterday,result['dates'])

    def test_related_older_parse_cache_is_removed(self):
        sha='a'*64
        self.write(f'files/_blob/{sha}.pdf')
        old=self.write(f'cache/pdf_parse/{sha}.1.120.0.json',days_ago=1)
        cleanup_downloads()
        self.assertFalse(old.exists())

    def test_preview_changes_no_files(self):
        pdf=self.write('files/cninfo/a.pdf')
        result=cleanup_downloads(execute=False)
        self.assertEqual(result['status'],'preview');self.assertTrue(pdf.exists())
        self.assertEqual(result['candidate_files'],1)

    def test_active_scan_defers_cleanup(self):
        pdf=self.write('files/cninfo/a.pdf')
        with download_session('active'):
            self.assertEqual(cleanup_downloads()['status'],'deferred')
            self.assertTrue(pdf.exists())
        cleanup_downloads();self.assertFalse(pdf.exists())

    def test_last_concurrent_scan_cleans_both_tasks_files(self):
        pdf1=self.write('files/cninfo/a.pdf');pdf2=self.write('files/hkexnews/b.pdf')
        with managed_download_session('last'):
            with managed_download_session('first'):
                self.assertTrue(pdf1.exists())
            self.assertTrue(pdf1.exists());self.assertTrue(pdf2.exists())
        self.assertFalse(pdf1.exists());self.assertFalse(pdf2.exists())

    def test_independent_process_lease_prevents_deletion(self):
        control=self.root/'download_cleanup';control.mkdir()
        code="import fcntl,sys; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_SH); print('ready',flush=True); sys.stdin.readline()"
        proc=subprocess.Popen([sys.executable,'-c',code,str(control/'session.lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(),'ready')
            pdf=self.write('files/cninfo/a.pdf')
            self.assertEqual(cleanup_downloads()['status'],'deferred')
            self.assertTrue(pdf.exists())
        finally:
            proc.communicate('\n',timeout=5)
        cleanup_downloads();self.assertFalse(pdf.exists())

    def test_cleanup_runs_after_successful_report_write(self):
        pdf=self.write('files/cninfo/a.pdf');report=self.root/'reports/finished.html'
        pipe=ScanPipeline('finished')
        def fake_run(query,started):
            self.assertTrue(pdf.exists())
            report.parent.mkdir();report.write_text('final report')
            return ScanResult('finished',TaskStatus.SUCCEEDED,html_path=str(report))
        pipe._run=fake_run
        result=pipe.run('test')
        self.assertEqual(result.status,TaskStatus.SUCCEEDED)
        self.assertFalse(pdf.exists());self.assertEqual(report.read_text(),'final report')

    def test_failed_scan_still_cleans_downloads(self):
        pdf=self.write('files/cninfo/a.pdf')
        pipe=ScanPipeline('failed')
        def fail(*args): raise ValueError('fixture failure')
        pipe._run=fail
        result=pipe.run('test')
        self.assertEqual(result.status,TaskStatus.FAILED)
        self.assertFalse(pdf.exists())

    def test_cleanup_failure_does_not_change_saved_result(self):
        with patch('app.core.download_cleanup.cleanup_downloads',side_effect=OSError('fixture failure')):
            with self.assertLogs('app.core.download_cleanup',level='ERROR'):
                with managed_download_session('saved'):
                    pass

    def test_hardlinked_bytes_not_counted_twice(self):
        a=self.write('files/cninfo/a.pdf',b'abcde')
        b=self.root/'files/_blob/hash.pdf';b.parent.mkdir();os.link(a,b)
        result=cleanup_downloads()
        self.assertEqual(result['removed_files'],2)
        self.assertEqual(result['released_bytes_estimate'],5)

    def test_external_hardlink_prevents_claiming_space_freed(self):
        a=self.write('files/cninfo/a.pdf',b'abcde');b=self.root/'retained.pdf';os.link(a,b)
        result=cleanup_downloads()
        self.assertEqual(result['released_bytes_estimate'],0)
        self.assertEqual(b.read_bytes(),b'abcde')

    def test_symlinked_files_and_directories_not_followed(self):
        outside=self.write('external/a.pdf')
        root=self.root/'files';root.mkdir()
        (root/'escape').symlink_to(outside.parent,target_is_directory=True)
        (root/'alias.pdf').symlink_to(outside)
        cleanup_downloads();self.assertTrue(outside.exists())

    def test_partial_delete_failure_keeps_pending_marker(self):
        pdf=self.write('files/cninfo/a.pdf')
        original=Path.unlink
        def denied(path,*args,**kw):
            if path==pdf: raise PermissionError('fixture denied')
            return original(path,*args,**kw)
        with patch.object(Path,'unlink',denied): result=cleanup_downloads()
        self.assertEqual(result['status'],'partial')
        self.assertTrue(list((self.root/'download_cleanup').glob('*.pending')))
        cleanup_downloads();self.assertFalse(pdf.exists())

    def test_metadata_log_records_deletion_without_modifying_reports(self):
        self.write('files/cninfo/a.pdf')
        cleanup_downloads(task_id='report1')
        logs=list((self.root/'download_cleanup').glob('*-report1.json'))
        self.assertEqual(len(logs),1)
        self.assertEqual(json.loads(logs[0].read_text())['removed_files'],1)


if __name__=='__main__': unittest.main()
