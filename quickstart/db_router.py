class TestLabDatabaseRouter:
    """Routes test_lab models to the sc_bot_test_lab database."""

    def db_for_read(self, model, **hints):
        if model._meta.app_label == 'test_lab':
            return 'sc_bot_test_lab'
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == 'test_lab':
            return 'sc_bot_test_lab'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == 'test_lab':
            return db == 'sc_bot_test_lab'
        return None
