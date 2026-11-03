"""同一套目录发现规则在 MySQL JSON_CONTAINS 与 SQLite JSON_EACH 下等价。"""
from test_review_mysql import mysql_case  # noqa: F401
from test_catalog_scaling import test_catalog_sql_visibility_matches_policy_and_project_removal as visibility_case
from test_catalog_scaling import test_catalog_paginates_in_sql_and_query_count_is_bounded as pagination_case
from test_review_discard import test_other_organizations_cannot_push_reviewers_batches_out_of_queue as review_queue_case


def test_mysql_catalog_json_visibility(mysql_case):
    app, client, actors = mysql_case
    visibility_case(app, client, actors)


def test_mysql_catalog_database_pagination(mysql_case):
    app, client, actors = mysql_case
    pagination_case(app, client, actors)


def test_mysql_review_queue_filters_membership_before_limit(mysql_case):
    app, client, actors = mysql_case
    review_queue_case(app, client, actors)
