class UserRepository:

    def __init__(self, db):
        self.db = db

    def get_by_email(self, email: str):
        return self.db.get_user_by_email(email)

    def get_lock_remaining(self, user):
        return self.db.lock_remaining(user)

    def record_failed_login(self, user_id, settings):
        self.db.record_failed_login(
            user_id,
            settings.max_failed_logins,
            settings.lockout_seconds
        )

    def reset_failed_logins(self, user_id):
        self.db.reset_failed_logins(user_id)