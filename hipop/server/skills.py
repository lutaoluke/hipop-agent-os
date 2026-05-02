"""
Skill 执行器：把工作流脚本包装成可调用函数
"""
import os
import sys
import subprocess

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
WF_DIR    = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workflows")
PYTHON    = sys.executable

def _run(script: str, args: list[str] = []) -> str:
    """运行工作流脚本，返回 stdout 输出"""
    cmd = [PYTHON, os.path.join(WF_DIR, script)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=PROJ_ROOT,
        timeout=600,   # 最长10分钟
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    if result.returncode != 0 and err:
        return f"⚠️ 执行出错：\n{err[-500:]}"
    return out or "✓ 执行完成，无输出"

def run_wf0(skus: list[str] = []) -> str:
    """在途库存 & 物流预估（空列表=全量扫描）"""
    return _run("wf0_logistics.py", skus)

def run_wf3() -> str:
    """销售周期分析"""
    return _run("wf3_sales_cycle.py")

def run_wf4() -> str:
    """补货建议"""
    return _run("wf4_replenishment.py")

SKILL_MAP = {
    "wf0_logistics": run_wf0,
    "wf3_sales":     run_wf3,
    "wf4_restock":   run_wf4,
}

def dispatch(skill: str, skus: list[str] = []) -> str:
    fn = SKILL_MAP.get(skill)
    if not fn:
        return f"未知 skill：{skill}"
    if skill == "wf0_logistics":
        return fn(skus)
    return fn()
