from __future__ import annotations

import pytest

from toe_dac import TDRepository, TDService


@pytest.fixture
def repository(tmp_path):
    return TDRepository(tmp_path / "data")


@pytest.fixture
def service(repository):
    return TDService.create(repository, "ut_test", retry_budget=2)


@pytest.fixture
def target():
    return {
        "positive": ["生成一个包含标题的文本产物"],
        "negative": ["不得修改工作目录以外的文件"],
        "acceptance_criteria": [
            {"criterion_id": "tc_001", "description": "产物包含标题", "required": True}
        ],
    }


@pytest.fixture
def observation():
    return {
        "facts": [
            {"fact_id": "f_001", "description": "目录可写", "source_type": "human_input"}
        ],
        "unknowns": [],
    }


@pytest.fixture
def estimate():
    return {
        "verdict": "feasible",
        "risks": [],
        "cost": {"max_attempts": 2},
        "information_gaps": [],
    }


@pytest.fixture
def two_action_plan():
    return {
        "plan_id": "plan_001",
        "version": 1,
        "actions": [
            {
                "action_id": "a_001",
                "objective": "创建文本产物",
                "depends_on": [],
                "instruction": "创建文本",
                "assertions": [{"assertion_id": "as_001", "description": "产物存在", "required": True}],
                "max_attempts": 2,
            },
            {
                "action_id": "a_002",
                "objective": "写入标题",
                "depends_on": ["a_001"],
                "instruction": "写入标题",
                "assertions": [{"assertion_id": "as_002", "description": "标题存在", "required": True}],
                "max_attempts": 2,
            },
        ],
    }


def advance_to_deciding(service, target, observation, estimate):
    service.start()
    service.submit_target(target)
    service.submit_observation(observation)
    service.submit_estimate(estimate)
    return service
