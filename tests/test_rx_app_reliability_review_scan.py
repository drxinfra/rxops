"""Проверки review_scan.py: python3 -m unittest discover -s tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "rx-app-reliability", "scripts", "review_scan.py")

SERVER = r'''
namespace Demo.Contracts.Server
{
  partial class ModuleFunctions
  {
    // SQL.CreateConnection() в комментарии не считается
    public virtual void Bad()
    {
      var connection = SQL.CreateConnection();
      var cmd = connection.CreateCommand();
      cmd.CommandText = "select 1 from t where id = " + id;
      cmd.ExecuteNonQuery();
    }

    public virtual void Good()
    {
      using (var connection = SQL.CreateConnection())
      using (var command = connection.CreateCommand())
      {
        command.CommandText = "select 1";
        command.ExecuteScalar();
      }
    }

    public virtual void Loops(List<long> ids)
    {
      foreach (var id in ids)
      {
        var doc = Contracts.Get(id);
        var tasks = Tasks.GetAll(t => t.Subject == "x").Where(t => t.Id > 0).ToList();
      }
      for (var i = 0; i < 10; i++)
        Contracts.GetAll(c => c.Id == i).FirstOrDefault();
      var all = Contracts.GetAll().ToList();
      var some = Contracts.GetAll().Where(c => c.Id > 5).ToList();
      var mem = Contracts.GetAll(c => c.Id > 1).ToList().Where(c => c.Name != null);
      foreach (var c in Contracts.GetAll())
        Logger.Debug("x");
      var s = "foreach (var z in Contracts.GetAll())";
    }

    [Remote]
    public virtual void RemoteTx()
    {
      Transactions.Execute(() => { });
    }

    public virtual void LockNoUnlock(IContract c)
    {
      Locks.Lock(c);
      c.Name = "x";
      c.Save();
    }

    public virtual void LockOk(IContract c)
    {
      var locked = false;
      try
      {
        locked = Locks.TryLock(c);
      }
      finally
      {
        if (locked)
          Locks.Unlock(c);
      }
    }
  }
}
'''

ASYNC = r'''
namespace Demo.Contracts.Server
{
  public class ModuleAsyncHandlers
  {
    public virtual void Recalc(Demo.Contracts.Server.AsyncHandlerInvokeArgs.RecalcInvokeArgs args)
    {
      var c = Contracts.Get(args.Id);
      Locks.Lock(c);
      System.Threading.Thread.Sleep(5000);
      Locks.Unlock(c);
    }

    public virtual void Polite(Demo.Contracts.Server.AsyncHandlerInvokeArgs.PoliteInvokeArgs args)
    {
      var c = Contracts.Get(args.Id);
      if (Locks.GetLockInfo(c).IsLockedByOther)
      {
        args.Retry = true;
        args.RetryReason = "locked";
        return;
      }
    }
  }
}
'''

HANDLERS = r'''
namespace Demo.Contracts
{
  partial class ContractServerHandlers
  {
    public override void AfterSave(Sungero.Domain.AfterSaveEventArgs e)
    {
      var h = AsyncHandlers.Recalc.Create();
      h.Id = _obj.Id;
      h.ExecuteAsync();
    }

    public override void BeforeSave(Sungero.Domain.BeforeSaveEventArgs e)
    {
      if (_obj.State.Properties.Status.IsChanged)
      {
        AsyncHandlers.Recalc.Create().ExecuteAsync();
      }
    }

    public override void Saving(Sungero.Domain.SavingEventArgs e)
    {
      Transactions.Execute(() => { _obj.Name = "x"; });
    }
  }
}
'''

CLIENT = r'''
namespace Demo.Contracts.Client
{
  partial class ModuleFunctions
  {
    public virtual void Many(List<long> ids)
    {
      foreach (var id in ids)
      {
        Functions.Module.Remote.Recalc(id);
      }
    }
  }
}
'''


class ReviewScanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rxops-scan-")
        files = {
            "Demo.Contracts.Server/ModuleServerFunctions.cs": SERVER,
            "Demo.Contracts.Server/ModuleAsyncHandlers.cs": ASYNC,
            "Demo.Contracts.Server/Contract/ContractHandlers.cs": HANDLERS,
            "Demo.Contracts.ClientBase/ModuleClientFunctions.cs": CLIENT,
            "Demo.Contracts.Server/obj/Generated.cs": "var c = SQL.CreateConnection();",
        }
        for rel, text in files.items():
            p = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_scan(self, *args):
        p = subprocess.run([sys.executable, SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode("utf-8")

    def findings(self, *extra):
        code, out = self.run_scan(self.dir, "--json", *extra)
        data = json.loads(out)
        return code, data, [(f["rule"], os.path.basename(f["file"]), f["line"]) for f in data["findings"]]

    def lines(self, found, rule, name):
        return sorted(l for r, n, l in found if r == rule and n == name)

    def test_sql_connection(self):
        code, data, found = self.findings()
        self.assertEqual(code, 1)
        self.assertEqual(self.lines(found, "sql-connection-no-using", "ModuleServerFunctions.cs"), [9],
                         "только Bad(); комментарий, using и obj/ не считаются")
        self.assertEqual(self.lines(found, "sql-concat", "ModuleServerFunctions.cs"), [11])
        self.assertEqual(data["cs_files"], 4)

    def test_loops_and_getall(self):
        _, _, found = self.findings()
        self.assertEqual(self.lines(found, "query-in-loop", "ModuleServerFunctions.cs"), [29, 30, 33])
        self.assertEqual(self.lines(found, "getall-unfiltered", "ModuleServerFunctions.cs"), [34, 37],
                         "GetAll().ToList() и foreach по GetAll(), но не GetAll().Where() и не строка")
        self.assertEqual(self.lines(found, "tolist-then-filter", "ModuleServerFunctions.cs"), [36])
        self.assertEqual(self.lines(found, "remote-in-loop", "ModuleClientFunctions.cs"), [10])

    def test_locks_transactions_handlers(self):
        _, _, found = self.findings()
        self.assertEqual(self.lines(found, "nested-transaction", "ModuleServerFunctions.cs"), [45])
        self.assertEqual(self.lines(found, "nested-transaction", "ContractHandlers.cs"), [23])
        self.assertEqual(self.lines(found, "lock-no-unlock", "ModuleServerFunctions.cs"), [50])
        self.assertEqual(self.lines(found, "lock-no-unlock", "ModuleAsyncHandlers.cs"), [9], "Unlock не в finally")
        self.assertEqual(self.lines(found, "lock-hard-in-handler", "ModuleAsyncHandlers.cs"), [9])
        self.assertEqual(self.lines(found, "thread-sleep", "ModuleAsyncHandlers.cs"), [10])
        self.assertEqual(self.lines(found, "async-no-retry", "ModuleAsyncHandlers.cs"), [6], "Polite() ставит Retry")
        self.assertEqual(self.lines(found, "executeasync-on-save", "ContractHandlers.cs"), [10],
                         "в BeforeSave вызов под условием")

    def test_text_output_rule_filter_and_index(self):
        code, out = self.run_scan(self.dir, "--rule", "async-no-retry,no-index-declared")
        self.assertEqual(code, 0, "только INFO")
        self.assertIn("ModuleAsyncHandlers.cs:6", out)
        self.assertIn("no-index-declared", out)
        with open(os.path.join(self.dir, "Demo.Contracts.Server", "ModuleQueries.xml"), "w", encoding="utf-8") as f:
            f.write("<q>CREATE INDEX IF NOT EXISTS idx_x ON t (a)</q>")
        code, out = self.run_scan(self.dir, "--rule", "no-index-declared")
        self.assertNotIn("no-index-declared", out)
        self.assertIn("объявлений индексов: 1", out)

    def test_mask_and_errors(self):
        p = os.path.join(self.dir, "Demo.Contracts.Server", "Secret.cs")
        with open(p, "w", encoding="utf-8") as f:
            f.write('class A { public void B() { var c = SQL.CreateConnection(); var s = "Password=Sup3rS3cret;"; } }\n')
        code, out = self.run_scan(p)
        self.assertEqual(code, 1)
        self.assertNotIn("Sup3rS3cret", out)
        code, out = self.run_scan(os.path.join(self.dir, "nope"))
        self.assertEqual(code, 2)
        empty = tempfile.mkdtemp(prefix="rxops-empty-")
        try:
            self.assertEqual(self.run_scan(empty)[0], 2)
        finally:
            shutil.rmtree(empty)
        self.assertEqual(self.run_scan("--help")[0], 0)
        self.assertEqual(self.run_scan(self.dir, "--rule", "nope")[0], 2)


if __name__ == "__main__":
    unittest.main()
