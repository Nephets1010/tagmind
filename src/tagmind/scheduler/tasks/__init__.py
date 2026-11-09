"""Celery 任务模块。

``celery_app.py`` 的 ``include`` 指向本包；阶段 1B 在此实现打标任务。
包必须存在且可导入，否则 worker / beat 会在启动时崩溃。
"""
