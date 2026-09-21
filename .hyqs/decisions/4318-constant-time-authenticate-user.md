# Job #4318: Constant-time authenticate_user

**Date:** 2026-09-08

This diff hardens user authentication against timing attacks by ensuring bcrypt password verification always runs, regardless of user eligibility. Previously, the code returned early if a user was inactive, non-existent, or lacked a password hash—patterns an attacker could detect through timing analysis. Now it computes bcrypt.checkpw against either the user's real hash or a pre-generated dummy hash, making the authentication flow constant-time and preventing leakage of which email addresses exist or are active. Five new tests verify bcrypt is called in all scenarios (unknown email, inactive user, OAuth-only user, wrong password, correct password) and one test validates the dummy hash itself.
This diff hardens user authentication against timing attacks by ensuring bcrypt password verification always runs, regardless of user eligibility. Previously, the code returned early if a user was inactive, non-existent, or lacked a password hash—patterns an attacker could detect through timing analysis. Now it computes bcrypt.checkpw against either the user's real hash or a pre-generated dummy hash, making the authentication flow constant-time and preventing leakage of which email addresses exist or are active. Five new tests verify bcrypt is called in all scenarios (unknown email, inactive user, OAuth-only user, wrong password, correct password) and one test validates the dummy hash itself. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
