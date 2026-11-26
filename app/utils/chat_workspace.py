"""会话工作区在 API 和维护 worker 之间的访问协调。"""

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

CHAT_WORKSPACE_REGISTRIES = {
    "log_analysis": "chat_log_analysis_sessions",
    "project_expert": "chat_project_expert_sessions",
    "package_search": "chat_package_search_sessions",
}
WORKSPACE_EXPIRED_MESSAGE = "会话工作区已过期，请重试。 / The session workspace has expired. Please retry."


@contextmanager
def workspace_lock(path: str | Path, *, exclusive: bool = False) -> Iterator[bool]:
    """锁住目录本身，避免共享卷上锁文件被删除或替换后产生两把锁。"""
    if fcntl is None:
        yield not exclusive
        return

    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            raise
        yield False
        return

    try:
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            current = Path(path).stat(follow_symlinks=False)
        except (BlockingIOError, FileNotFoundError):
            yield False
        else:
            opened = os.fstat(fd)
            locked = (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
            if locked and not exclusive:
                # 前置处理也算一次会话活动，即使尚未写入运行记录。
                os.utime(fd)
            yield locked
    finally:
        os.close(fd)
