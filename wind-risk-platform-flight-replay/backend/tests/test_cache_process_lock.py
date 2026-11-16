import multiprocessing as mp
import time
import numpy as np


def read_product(counter, output):
    from app.forecast_store import Product, IOStats
    from app.wind_provider import WindGrid
    grid = WindGrid(np.arange(3.), np.arange(3.), np.ones((3,3)), np.zeros((3,3)),
                    'test', 0, '100m AGL', 'test')
    def decode():
        with counter.get_lock():
            counter.value += 1
        time.sleep(.1)
        return grid
    product = Product('shared-cold-source', '100m AGL', 'wrf', decode, IOStats(), grid)
    output.put(float(product.grid().u[1,1]))


def test_two_spawned_decoders_publish_one_immutable_product(monkeypatch,tmp_path):
    monkeypatch.setenv('FORECAST_DERIVED_CACHE_DIR',str(tmp_path))
    context = mp.get_context('spawn')
    counter, output = context.Value('i',0), context.Queue()
    workers = [context.Process(target=read_product,args=(counter,output)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        assert [output.get(timeout=10) for _ in workers] == [1.,1.]
        for worker in workers:
            worker.join(3)
            assert worker.exitcode == 0
        assert counter.value == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(1)
        output.close()
