import threading

import locks
from runner import runner


ID_A = "sbx_" + "a" * 32
ID_B = "sbx_" + "b" * 32


def test_same_sandbox_gets_the_same_lock():
    assert locks.lock_for(ID_A) is locks.lock_for(ID_A)


def test_different_sandboxes_get_different_locks():
    assert locks.lock_for(ID_A) is not locks.lock_for(ID_B)


def test_threads_asking_at_once_all_get_one_lock():
    sandbox_id = "sbx_" + "c" * 32
    got = []
    start = threading.Barrier(20)

    def ask():
        start.wait()
        got.append(locks.lock_for(sandbox_id))

    threads = [threading.Thread(target=ask) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(lock) for lock in got}) == 1


def test_stop_turn_with_no_turn_running_returns_without_taking_the_lock():
    lock = locks.lock_for(ID_A)
    with lock:
        runner.stop_turn(ID_A)
