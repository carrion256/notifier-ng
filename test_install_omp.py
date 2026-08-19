#!/usr/bin/env python3
"""Regression tests for OMP hook installation."""

import tempfile
import unittest
from pathlib import Path

import install


class OmpHookInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hooks = Path(self.temp.name) / "hooks" / "post"

    def install_hook(self):
        plan = install.Plan(apply=True)
        install.step_omp_hook(plan, self.hooks)
        self.assertEqual(plan.refused, 0, plan.lines)
        return plan, self.hooks / install.OMP_HOOK_NAME

    def test_fresh_install_is_regular_wrapper_not_symlink(self):
        _plan, target = self.install_hook()
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink(), "OMP ambient discovery skips symlinks")
        self.assertEqual(target.read_text(), install._omp_wrapper())

    def test_legacy_symlink_is_migrated_to_regular_wrapper(self):
        self.hooks.mkdir(parents=True)
        target = self.hooks / install.OMP_HOOK_NAME
        target.symlink_to(install.OMP_EXTENSION)

        plan, target = self.install_hook()

        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), install._omp_wrapper())
        self.assertTrue(
            any(kind == "change" and "[replace]" in text and "legacy symlink" in text for kind, text in plan.lines),
            plan.lines,
        )

    def test_reapply_is_noop(self):
        _plan, target = self.install_hook()
        before = target.read_bytes()

        second = install.Plan(apply=True)
        install.step_omp_hook(second, self.hooks)

        self.assertEqual(second.refused, 0, second.lines)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(any(kind == "noop" for kind, _text in second.lines), second.lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
