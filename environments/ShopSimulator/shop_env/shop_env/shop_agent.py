import json
import logging
import os
from typing import Dict, Any, Optional

# Constants
MAX_HISTORY_LENGTH = 42
LOG_FILE = "shop_agent.log"

# Configure logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Shopper Simulator 隐藏事实落盘目录（仅服务端可读）。
# 未设置时不落盘（单轮模式行为不变）。
_FACTS_DIR = os.environ.get("SHOPSIM_FACTS_DIR", "").strip()


def _dump_shopper_facts(env: Any, task_idx: int) -> None:
    """把本任务的隐藏需求写到事实文件，供 Shopper Simulator 读取。

    只包含"用户想要什么"（完整需求、属性、目标规格、预算、画像与推理），
    不包含 gold asin / reward 逻辑，防止 shopper 指路商品。
    """
    if not _FACTS_DIR:
        return
    try:
        goal = env.server.goals[task_idx]
        persona = goal.get("user_persona") or {}
        facts = {
            "idx": task_idx,
            "instruction_full": goal.get("instruction_full", ""),
            "attributes": goal.get("attributes", []),
            "goal_options": goal.get("goal_options", []),
            "price_upper": goal.get("price_upper"),
            "persona": {k: v for k, v in persona.items() if k != "__reasoning__"},
            "persona_reasoning": persona.get("__reasoning__"),
            "reason_key": goal.get("reason_key"),
        }
        os.makedirs(_FACTS_DIR, exist_ok=True)
        path = os.path.join(_FACTS_DIR, f"{task_idx}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False, indent=2)
        logger.info(f"[Facts] wrote shopper facts for task {task_idx}")
    except Exception:
        logger.exception(f"[Facts] failed to dump facts for task {task_idx}")


def _handle_reset_action(env: Any, env_idx: int, task_idx: Optional[int]) -> Dict[str, Any]:
    """
    Handle reset action

    Args:
        env: Environment object
        env_idx: Environment index
        task_idx: Task index

    Returns:
        Dictionary containing reset information
    """
    logger.info(f"[Reset] Starting task {task_idx}, environment index: {env_idx}")
    message = f"Task {task_idx} started"
    env.reset(idx=task_idx)
    _dump_shopper_facts(env, task_idx)
    return_info = {
        'instruction': env.instruction_text,
        'instruction_simple': env.instruction_simple,
        'goal_options': env.goal_options,
        'message': message,
        'env_idx': env_idx,
        'idx': task_idx,
        'environment_version': getattr(
            env.server,
            "environment_version",
            "shopsimulator-environment-v2.1",
        ),
        "observation_state": env.structured_observation(),
    }
    if hasattr(env, 'user_persona') and env.user_persona is not None:
        return_info['user_persona'] = env.user_persona
        return_info['reason_key'] = env.reason_key
    return return_info


def _extract_action_from_response(response: str) -> str:
    """
    Extract action from response

    Args:
        response: Raw response string

    Returns:
        Extracted action string
    """
    if "\nAction: " in response:
        return response.split("\nAction: ")[1]
    return response


def _format_available_actions(available_actions: Dict[str, Any]) -> str:
    """
    Format available actions information

    Args:
        available_actions: Dictionary of available actions

    Returns:
        Formatted action text
    """
    clickables = available_actions['clickables'].copy()
    if "search" in clickables:
        clickables.remove("search")

    return (
        f"\n\n搜索功能是否可用: {available_actions['has_search_bar']}"
        f"\n\n可点击的按钮: {json.dumps(clickables, ensure_ascii=False)}"
    )


def _handle_interact_action(
    env: Any,
    env_idx: int,
    response: str
) -> Dict[str, Any]:
    """
    Handle interact action

    Args:
        env: Environment object
        env_idx: Environment index
        response: Response string

    Returns:
        Dictionary containing interaction information
    """
    logger.info(
        f"[Interact] Received response: {response}, "
        f"environment index: {env_idx}, Session ID: {env.session}"
    )

    # Normalize response format
    normalized_response = response.replace("\\n", "\n")
    env.history.append({'role': 'assistant', 'content': normalized_response})

    # Extract action
    action_str = _extract_action_from_response(normalized_response)

    # Execute environment step
    observation, status, info = env.step(action_str)
    done, reward = status['done'], status['reward']

    # Get and format available actions
    available_actions = env.get_available_actions()
    action_text = _format_available_actions(available_actions)
    observation = observation + action_text

    # Extract status information
    if done:
        reward_detail = status["reward_detail"]
        purchase = status['purchase']
        goal = status['goal']
    else:
        reward_detail = {}
        purchase = {}
        goal = {}

    # Build return information
    return_info = {
        "done": done,
        "reward": reward,
        "instruction": observation,
        "message": "Continue interaction",
        "env_idx": env_idx,
        "idx": env.session,
        "reward_detail": reward_detail,
        "purchase": purchase,
        "goal": goal,
        "over": len(env.history) > MAX_HISTORY_LENGTH or done,
        "observation_state": env.structured_observation(),
    }
    if done:
        return_info["termination_reason"] = status.get(
            "termination_reason", "environment_done"
        )
        return_info["reward_valid"] = bool(status.get("reward_valid", True))
    if status.get("progress") is not None:
        return_info["progress"] = status["progress"]

    return return_info


def shop_agent(
    env: Any,
    env_idx: int,
    action: str,
    idx: Optional[int] = None,
    response: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main shop agent function that handles environment reset and interaction actions

    Args:
        env: Environment object
        env_idx: Environment index
        action: Action type ("reset" or "interact")
        idx: Task index, only used for reset action
        response: Response string, only used for interact action

    Returns:
        Dictionary containing action processing results

    Raises:
        ValueError: When action is not "reset" or "interact"
    """
    if action == "reset":
        if idx is None:
            raise ValueError("reset action requires idx parameter")
        return _handle_reset_action(env, env_idx, idx)

    elif action == "interact":
        if response is None:
            raise ValueError("interact action requires response parameter")
        return _handle_interact_action(env, env_idx, response)

    else:
        raise ValueError(f"Unknown action type: {action}, supported actions: 'reset', 'interact'")
