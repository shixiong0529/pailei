"""End-of-scan cleanup of downloaded PDFs and PDF text caches.

An advisory shared lease spans the whole scan (Web and CLI). Cleanup takes an
exclusive, nonblocking lease, so one report cannot delete another scan's input.
Date markers survive deferred cleanup, midnight and interrupted scan processes.
Financial Raw JSON, report files, database and model cache are outside the allowlist.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
import fcntl
import json
import logging
import os
import re

from app.config import settings

log = logging.getLogger(__name__)


def _control_dir():
    directory = settings.db_path.parent / 'download_cleanup'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _mark_today(directory):
    day = datetime.now().astimezone().date().isoformat()
    (directory / f'{day}.pending').touch(exist_ok=True)
    return day


@contextmanager
def download_session(task_id: str):
    """Keep all downloaded inputs alive until report/DB writes and verification end."""
    directory = _control_dir()
    with (directory / 'session.lock').open('a+b') as lease:
        fcntl.flock(lease, fcntl.LOCK_SH)
        try:
            _mark_today(directory)
            yield
        finally:
            try:
                _mark_today(directory)
            except OSError:
                log.exception('Unable to update pending cleanup date')
            finally:
                fcntl.flock(lease, fcntl.LOCK_UN)


@contextmanager
def managed_download_session(task_id: str):
    try:
        with download_session(task_id):
            yield
    finally:
        try:
            summary = cleanup_downloads(task_id=task_id)
            log.info('Download cleanup for %s: %s', task_id, summary['status'])
        except Exception:
            # Cleanup cannot turn a saved report into a failed task.
            log.exception('Download cleanup failed for %s; pending dates retained', task_id)


def _files(root):
    if not root.exists() or root.is_symlink():
        return
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(parent)/d).is_symlink()]
        for name in names:
            path = Path(parent)/name
            if path.is_file() and not path.is_symlink():
                yield path


def _candidates(days):
    candidates = []
    removed_hashes = set()
    pdf_root = settings.files_dir
    for path in _files(pdf_root):
        if not (path.name.endswith('.pdf') or path.name.endswith('.pdf.part')):
            continue
        st = path.stat()
        if datetime.fromtimestamp(st.st_mtime).astimezone().date().isoformat() not in days:
            continue
        candidates.append((path, pdf_root, st))
        if re.fullmatch(r'[a-fA-F0-9]{64}', path.stem):
            removed_hashes.add(path.stem)
    cache_root = settings.cache_dir/'pdf_parse'
    for path in _files(cache_root):
        # Includes successful parser JSON and interrupted atomic-write temp files.
        if path.suffix != '.json' and not path.name.startswith('tmp'):
            continue
        st = path.stat()
        if (datetime.fromtimestamp(st.st_mtime).astimezone().date().isoformat() in days
                or path.name.split('.')[0] in removed_hashes):
            candidates.append((path, cache_root, st))
    return candidates


def cleanup_downloads(*, task_id='manual', execute=True):
    directory = _control_dir()
    with (directory/'session.lock').open('a+b') as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'deferred', 'reason':'another scan is using downloaded files', 'removed_files':0}
        try:
            today = _mark_today(directory) if execute else datetime.now().astimezone().date().isoformat()
            days = {today}
            for marker in directory.glob('*.pending'):
                try:
                    day = date.fromisoformat(marker.stem).isoformat()
                except ValueError:
                    continue
                if day <= today:
                    days.add(day)
            candidates = _candidates(days)
            summary = {'status':'completed' if execute else 'preview', 'task_id':task_id,
                       'dates':sorted(days), 'at':datetime.now().astimezone().isoformat(timespec='seconds'),
                       'candidate_files':len(candidates), 'removed_files':0,
                       'released_bytes_estimate':0, 'removed':[], 'errors':[]}
            if not execute:
                summary['paths'] = [str(p) for p, _, _ in candidates]
                return summary
            inodes = {}
            for path, root, old in candidates:
                try:
                    # Fail closed if an external process replaced a file or inserted a symlink.
                    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                        raise OSError('path changed or escaped cleanup directory')
                    current = path.stat()
                    if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (old.st_dev, old.st_ino, old.st_mtime_ns, old.st_size):
                        raise OSError('file changed after cleanup planning')
                    path.unlink()
                    summary['removed_files'] += 1
                    summary['removed'].append(str(path))
                    inode = inodes.setdefault((old.st_dev, old.st_ino), {'links':old.st_nlink,'removed':0,'size':old.st_size})
                    inode['links'] = max(inode['links'], old.st_nlink)
                    inode['removed'] += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    summary['errors'].append({'path':str(path), 'reason':str(exc)})
            summary['released_bytes_estimate'] = sum(i['size'] for i in inodes.values() if i['removed'] >= i['links'])
            if summary['errors']:
                summary['status']='partial'
            # Metadata only; no report or financial Raw data is edited.
            safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', task_id)[:100]
            (directory/f'{today}-{safe_id}.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2), encoding='utf-8')
            if not summary['errors']:
                for day in days:
                    (directory/f'{day}.pending').unlink(missing_ok=True)
            return summary
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)
