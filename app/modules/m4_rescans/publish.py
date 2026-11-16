"""发布阶段的故障注入点。

正常情况下 FAULT 为 None。测试或演练把 FAULT 设为可调用对象，
publish 事务在切换采纳页之后、提交之前调用它；抛出的异常会回滚
整个发布事务，决定停留在 PENDING，可再次发布恢复。
"""

class PublishInterrupted(Exception):
    pass


FAULT = None


def tick():
    if FAULT is not None:
        FAULT()
