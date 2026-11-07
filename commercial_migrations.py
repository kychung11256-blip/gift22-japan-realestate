import authz
import commercial_media as media

def migrate(conn):
    """Idempotent auth/media migration. Existing listings/clients are never deleted."""
    authz.init_auth_schema(conn)
    media.ensure_schema(conn)
    conn.commit()

# Password hashing intentionally exported; account creation is parent-owned.
password_hash = authz.password_hash
batch_existing_published = media.batch_existing_published
