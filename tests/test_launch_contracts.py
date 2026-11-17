"""M12 launch-source contracts, using M00's fixed synthetic source copies only."""
import ast
import hashlib
from pathlib import Path
import re
import unittest

import isolation


# master c546f34491a937d10ad99bfec2b26e8821876836, unchanged registration source.
# Git blob: dddc3262b17b3c204a126bf063bba034dddf8942; hash includes original bytes.
REGISTRATION_SHA256 = '0d7f2bf862c3970763333bbeea170efbed622220666f9a0a4383e45160194ed9'


class LaunchContracts(unittest.TestCase):
    def test_wrapper_sources_and_schedule_identity(self):
        code = Path(isolation.ACTIVE.config['code'])
        self.assertEqual(Path(__file__).resolve().parents[1], code)
        names = ('dashboard.ps1', 'daily.ps1', 'scheduled-market.ps1',
                 'register-morning-task.ps1')
        raw = {name: (code / name).read_bytes() for name in names}
        source = {name: body.decode('utf-8-sig') for name, body in raw.items()}

        def assert_forwarded_flags(wrapper, *python_sources):
            accepted = set()
            for relative in python_sources:
                tree = ast.parse((code / relative).read_text(encoding='utf-8-sig'))
                for call in ast.walk(tree):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and call.func.attr == 'add_argument'):
                        accepted.update(arg.value for arg in call.args
                                        if isinstance(arg, ast.Constant)
                                        and isinstance(arg.value, str))
            forwarded = set(re.findall(r'''['"]\s*(--[a-z][a-z0-9-]*)\s*['"]''', wrapper))
            self.assertTrue(forwarded)
            self.assertEqual(forwarded - accepted, set())

        dashboard = source['dashboard.ps1']
        daily = source['daily.ps1']
        scheduled = source['scheduled-market.ps1']
        for name, interpreter in (('dashboard.ps1', 'python.exe'), ('daily.ps1', 'python.exe'),
                                  ('scheduled-market.ps1', 'pythonw.exe')):
            with self.subTest(wrapper=name):
                self.assertIn("Join-Path $PSScriptRoot '.venv\\Scripts\\" + interpreter + "'", source[name])
                self.assertIn('Test-Path -LiteralPath $interpreter', source[name])
                self.assertNotIn('Register-ScheduledTask', source[name])

        self.assertIn("'scripts\\dashboard_startup.py'", dashboard)
        self.assertRegex(dashboard, r"(?m)^\$arguments = .*'--desktop'.*'--saved-accounts'")
        self.assertIn("if ($Offline) { $arguments += '--offline' }", dashboard)
        self.assertIn("elseif (-not $SavedMarket) { $arguments += '--daily-refresh' }", dashboard)
        self.assertIn("if ($RetryMarket) { $arguments += '--retry-market' }", dashboard)
        self.assertIn('if ($RetryMarket -and ($SavedMarket -or $Offline))', dashboard)
        self.assertIn('& $interpreter @arguments', dashboard)
        self.assertIn('if ($LASTEXITCODE -ne 0) { throw', dashboard)
        assert_forwarded_flags(dashboard, 'scripts/launch/startup.py', 'scripts/web/server.py')

        self.assertIn("'scripts\\market\\daily.py'", daily)
        for flag, argument in (('source', 'Source'), ('verify', 'Verify'),
                               ('recover', 'Recover'), ('nh-evidence', 'NHEvidence')):
            self.assertIn("@('--" + flag + "', $" + argument + ')', daily)
        self.assertIn('& $interpreter @arguments', daily)
        self.assertIn('exit $LASTEXITCODE', daily)
        assert_forwarded_flags(daily, 'scripts/market/daily.py')

        self.assertIn("'scripts\\scheduled_market.py'", scheduled)
        self.assertIn("if ($Check) { $arguments += ' --check' }", scheduled)
        self.assertRegex(scheduled, r'Start-Process[^\r\n]*-WindowStyle Hidden')
        self.assertIn('-FilePath $interpreter -ArgumentList $arguments -WorkingDirectory $PSScriptRoot', scheduled)
        self.assertIn('$processHandle = $process.Handle', scheduled)
        self.assertIn('$process.WaitForExit()', scheduled)
        self.assertIn('$resultCode = $process.ExitCode', scheduled)
        self.assertIn("if ($null -eq $resultCode) { throw", scheduled)
        self.assertIn('$record.exit_code = $resultCode', scheduled)
        self.assertIn('$process.Dispose()', scheduled)
        self.assertRegex(scheduled, r'(?m)^exit \$resultCode\s*$')
        assert_forwarded_flags(scheduled, 'scripts/launch/scheduled.py')

        self.assertEqual(hashlib.sha256(raw['register-morning-task.ps1']).hexdigest(),
                         REGISTRATION_SHA256)
        registration = source['register-morning-task.ps1']
        self.assertIn("$taskName = 'InvestAgent-Morning-0640-KST'", registration)
        self.assertIn('$kstNow.Day, 6, 40, 0', registration)
        self.assertIn('<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>', registration)
