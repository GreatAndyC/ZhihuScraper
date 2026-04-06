import platform
import subprocess


def perform_post_task_action(action: str) -> tuple[bool, str]:
    if action in {"", "none", None}:
        return True, "未设置任务完成后的系统动作"

    system = platform.system().lower()
    commands: list[list[str]] = []

    if action == "display_off":
        if system == "darwin":
            commands = [["pmset", "displaysleepnow"]]
        elif system == "windows":
            commands = [[
                "powershell",
                "-NoProfile",
                "-Command",
                "(Add-Type '[DllImport(\"user32.dll\")]public static extern int SendMessage(int hWnd,int hMsg,int wParam,int lParam);' -Name a -Pas)::SendMessage(-1,0x0112,0xF170,2)",
            ]]
        else:
            commands = [["xset", "dpms", "force", "off"]]
    elif action == "sleep":
        if system == "darwin":
            commands = [["pmset", "sleepnow"]]
        elif system == "windows":
            commands = [["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]]
        else:
            commands = [["systemctl", "suspend"]]
    else:
        return False, f"未知的系统动作: {action}"

    for command in commands:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            return True, f"已执行系统动作: {action}"
        except Exception as exc:
            last_error = str(exc)
    return False, f"执行系统动作失败: {last_error}"
