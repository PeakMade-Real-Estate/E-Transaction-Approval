import unittest

from flask import session

import app


class BankAccountPermissionTests(unittest.TestCase):
    def test_treasury_can_view_and_edit(self):
        with app.app.test_request_context('/'):
            session['role'] = 'treasury'
            self.assertTrue(app.can_view_bank_accounts())
            self.assertTrue(app.can_edit_bank_accounts())

    def test_treasury_backup_can_view_and_edit(self):
        with app.app.test_request_context('/'):
            session['role'] = 'treasury_bank_admin'
            self.assertTrue(app.can_view_bank_accounts())
            self.assertTrue(app.can_edit_bank_accounts())

    def test_vp_and_cfo_are_read_only(self):
        for role in ('vp', 'cfo'):
            with self.subTest(role=role):
                with app.app.test_request_context('/'):
                    session['role'] = role
                    self.assertTrue(app.can_view_bank_accounts())
                    self.assertFalse(app.can_edit_bank_accounts())

    def test_other_roles_have_no_access(self):
        for role in ('submitter', 'sam', 'controller', 'business_admin', 'it_admin'):
            with self.subTest(role=role):
                with app.app.test_request_context('/'):
                    session['role'] = role
                    self.assertFalse(app.can_view_bank_accounts())
                    self.assertFalse(app.can_edit_bank_accounts())


class BankAccountValidationTests(unittest.TestCase):
    def valid_data(self):
        return {
            'entity_key': '3',
            'bank_name': 'Wells Fargo Bank',
            'account_number': '4567890123',
            'status': 'Open',
            'date_opened': '2026-09-10',
        }

    def test_new_account_requires_core_fields(self):
        data = {'entity_key': '', 'bank_name': '', 'account_number': '', 'status': '', 'date_opened': ''}
        errors = app._validate_bank_account_form(data, is_new=True)
        self.assertGreaterEqual(len(errors), 5)

    def test_legacy_edit_may_have_blank_date_opened(self):
        data = self.valid_data()
        data['account_number'] = ''
        data['date_opened'] = ''
        errors = app._validate_bank_account_form(data, is_new=False)
        self.assertEqual(errors, [])

    def test_closed_account_requires_date_closed(self):
        data = self.valid_data()
        data['status'] = 'Closed'
        data['date_closed'] = ''
        errors = app._validate_bank_account_form(data, is_new=False)
        self.assertIn('Date Closed is required when closing a bank account.', errors)

    def test_closed_account_with_date_closed_is_valid(self):
        data = self.valid_data()
        data['status'] = 'Closed'
        data['date_closed'] = '2026-09-10'
        errors = app._validate_bank_account_form(data, is_new=False)
        self.assertEqual(errors, [])

    def test_legacy_active_status_is_rejected(self):
        # Batch 2 status vocabulary cleanup: only Open/Closed are canonical now.
        data = self.valid_data()
        data['status'] = 'Active'
        errors = app._validate_bank_account_form(data, is_new=True)
        self.assertIn('Status must be Open or Closed.', errors)


if __name__ == '__main__':
    unittest.main()
