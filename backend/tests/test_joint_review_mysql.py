from test_review_mysql import mysql_case as mysql_case
from test_joint_review import test_separate_roles_joint_dedup_preserves_raw_and_exports_reviewed_representative as exercise_joint

def test_real_mysql_joint_role_and_dedup_delivery(mysql_case):
    app,client,actors=mysql_case
    exercise_joint(app,client,actors)
