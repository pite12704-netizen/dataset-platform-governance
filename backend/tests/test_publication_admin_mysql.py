"""Publication role boundaries on a disposable, explicitly isolated MySQL."""
from test_review_mysql import mysql_case  # noqa: F401
from test_publication_admin import (
    test_organization_publication_is_admin_only_and_revocation_is_immediate as organization_case,
    test_organization_admin_cannot_approve_public_listing as public_case,
)

def platform_actor(app,actors):
    # This fixture creates every account without platform privileges by default.
    from app.models import User
    with app.state.db.Session() as db:
        db.get(User,actors['admin']['id']).platform_admin=True
        db.commit()

def test_mysql_admin_only_publication(mysql_case):
    app,client,actors=mysql_case
    platform_actor(app,actors)
    organization_case(client,actors)

def test_mysql_public_listing_requires_platform_admin(mysql_case):
    app,client,actors=mysql_case
    platform_actor(app,actors)
    public_case(client,actors)
