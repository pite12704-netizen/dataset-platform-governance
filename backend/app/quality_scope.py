"""Public, versioned task scope. A quality tier is not a universal accuracy claim."""


def person_clothing_scope():
    return {'id':'person-clothing-v1','label_schema_version':1,
        'assessed':['person_iou50','hat','short_sleeves'],'not_assessed':['upper_color'],
        'statement':'本任务验收覆盖人物定位、帽子和袖长；上衣颜色未进行独立统计验收。实际是否达标以有效评测报告为准。'}
