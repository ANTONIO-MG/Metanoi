"""Database routing: everything on the PostgreSQL primary, MySQL as a backup.

Registered in ``DATABASE_ROUTERS``; see :class:`PrimaryReplicaRouter`.
"""


class PrimaryReplicaRouter:
    """
    Database router for the Metanoia Group project.

    Topology:
        - ``default`` -> PostgreSQL  (PRIMARY: all reads and writes)
        - ``backup``  -> MySQL       (BACKUP / DR server)

    All ORM reads and writes go to the PostgreSQL ``default`` connection.
    The MySQL ``backup`` connection is kept schema-compatible (run
    ``python manage.py migrate --database=backup``) so it can serve as a
    disaster-recovery target / reporting replica.  Application code never
    talks to it implicitly; use ``.using('backup')`` explicitly if needed.
    """

    primary_db = "default"
    backup_db = "backup"

    def db_for_read(self, model, **hints):
        """Every read goes to the primary."""
        return self.primary_db

    def db_for_write(self, model, **hints):
        """Every write goes to the primary."""
        return self.primary_db

    def allow_relation(self, obj1, obj2, **hints):
        """Always allowed: both connections hold the same schema."""
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Migrate both connections: the primary by default, the backup when
        explicitly targeted with ``migrate --database=backup``.
        """
        return db in (self.primary_db, self.backup_db)
