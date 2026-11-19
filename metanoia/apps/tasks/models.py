"""The tasks app has been removed (client request, 2026-11-09).

Nothing is defined here any more. The app stays in ``INSTALLED_APPS`` for one
release only so that migration ``orgtasks.0010`` can drop the ``orgtasks_task``
and ``orgtasks_taskassignment`` tables, and because applied migrations in
``accounts`` and ``myhub`` name ``orgtasks`` migrations as dependencies.
"""
