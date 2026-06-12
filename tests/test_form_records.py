import unittest

from src.text_cleaner import build_form_records_text, build_resolution_text


class FormRecordsTextTest(unittest.TestCase):
    def test_cleans_and_labels_form_fields(self):
        records = [
            {
                "source": "form_develop_finish",
                "label": "研发完成",
                "fields": [
                    {
                        "name": "function_description",
                        "label": "功能修改",
                        "value": "<p>修复重复配置项</p>",
                    },
                    {
                        "name": "test_point",
                        "label": "测试要点",
                        "value": "<p>从 1.6.0 升级到 1.6.1</p>",
                    },
                ],
            }
        ]

        text = build_form_records_text(records)

        self.assertEqual(
            text,
            "[研发完成] 功能修改: 修复重复配置项 | 测试要点: 从 1.6.0 升级到 1.6.1",
        )

    def test_skips_empty_placeholders_and_duplicate_rows(self):
        record = {
            "source": "form_tester_verify",
            "label": "测试验证",
            "fields": [
                {"name": "test_point", "label": "测试要点", "value": "无"},
                {
                    "name": "test_result_description",
                    "label": "结果说明",
                    "value": "<p>验证通过</p>",
                },
            ],
        }

        text = build_form_records_text([record, record])

        self.assertEqual(text, "[测试验证] 结果说明: 验证通过")

    def test_combines_journal_and_form_resolution(self):
        text = build_resolution_text(
            "已发布修复包",
            "[测试验证] 结果说明: 升级验证通过",
        )

        self.assertIn("[处理记录] 已发布修复包", text)
        self.assertIn("[测试验证] 结果说明: 升级验证通过", text)

    def test_maps_boolean_workflow_results(self):
        records = [
            {
                "source": "form_tester_verify",
                "label": "测试验证",
                "fields": [
                    {"name": "test_result", "label": "测试结果", "value": "1"},
                ],
            }
        ]

        self.assertEqual(
            build_form_records_text(records),
            "[测试验证] 测试结果: 通过",
        )

        records[0]["fields"][0]["value"] = 0
        self.assertEqual(
            build_form_records_text(records),
            "[测试验证] 测试结果: 不通过",
        )


if __name__ == "__main__":
    unittest.main()
