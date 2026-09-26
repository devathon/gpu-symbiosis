import asyncio, sys
sys.path.insert(0, "tests")
import test_e2e_core_loop as t
srv = t.Server(t._cfg())
asyncio.run(srv.serve())
