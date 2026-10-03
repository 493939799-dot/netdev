#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  设备工具台（双击即用）
#  给"不想敲命令"的场景：体检 / 接入 / 备份 / 恢复 / 管理快照 / 打包
#  所有动作都调用 netdev，和网页里点按钮是同一套逻辑
#  2026-10-01：打开菜单时会自动确保网页服务在跑（历史高频故障："服务又起不来了"）
# ═══════════════════════════════════════════════════════════════
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT_DEFAULT="$(cd "$HERE/.." && pwd)"
NETDEV_ROOT="${NETDEV_ROOT:-$ROOT_DEFAULT}"
ROOT="$NETDEV_ROOT"
NETDEV="$ROOT/netdev"
export PATH="$HOME/bin:$ROOT/bin:$HOME/homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$ROOT" || exit 1
C_B="\033[1m"; C_D="\033[2m"; C_G="\033[32m"; C_Y="\033[33m"; C_R="\033[31m"; C_X="\033[0m"

pause() { printf "\n${C_D}按回车返回菜单…${C_X}"; read -r _ || true; }

# ── 网页服务：进菜单先确保它在跑（幂等，已在跑则秒回）────────────
SVC_OK=no; SVC_STATE="未启动"
svc_refresh() {
  if "$NETDEV" ui status >/dev/null 2>&1; then
    SVC_OK=yes; SVC_STATE="运行中 http://127.0.0.1:8898"
    return 0
  fi
  local out; out="$("$NETDEV" ui start 2>&1)"
  if "$NETDEV" ui status >/dev/null 2>&1; then
    SVC_OK=yes; SVC_STATE="已自动拉起 http://127.0.0.1:8898"
  else
    SVC_OK=no;  SVC_STATE="起不来（看 ${ROOT}/logs/ui-service.log）"
    printf "${C_Y}%s${C_X}\n" "$out" >&2
  fi
}

banner() {
  clear
  printf "${C_B}╔══════════════════════════════════════════════════════════╗${C_X}\n"
  printf "${C_B}║            设备工具台 · netdev 控制台                    ║${C_X}\n"
  printf "${C_B}╚══════════════════════════════════════════════════════════╝${C_X}\n"
  if [ "$SVC_OK" = yes ]; then
    printf "  网页服务：${C_G}%s${C_X}\n" "$SVC_STATE"
  else
    printf "  网页服务：${C_R}%s${C_X}\n" "$SVC_STATE"
  fi
  printf "  ${C_D}说明书：  $ROOT/README-从这里开始.txt${C_X}\n\n"
}

menu() {
  banner
  printf "  1) ${C_B}体检${C_X}        检查工具/服务/串口/快照是否正常\n"
  printf "  2) ${C_B}接入设备${C_X}    列出设备与会话（接入建议用网页，更直观）\n"
  printf "  3) ${C_B}备份配置${C_X}    给当前设备存快照（接客户设备第一步）\n"
  printf "  4) ${C_B}恢复配置${C_X}    按快照还原（会先给你看差异，再确认）\n"
  printf "  5) ${C_B}快照管理${C_X}    查看/删除/取回快照\n"
  printf "  6) ${C_B}打开网页${C_X}    确保服务在跑并打开浏览器（127.0.0.1:8898）\n"
  printf "  7) ${C_B}一键打包${C_X}    把全部配置打包到桌面（防误删）\n"
  printf "  8) ${C_B}修复按钮${C_X}    网页终端页命令栏丢了就点这个\n"
  printf "  9) ${C_B}网页服务${C_X}    启动 / 重启 / 停止 / 看日志\n"
  printf "  0) 退出\n"
  printf "\n  ${C_Y}你的选择：${C_X}"
}

svc_refresh          # 开局就把服务搞定，后面用网页/报告都不用再操心

while true; do
  menu
  read -r choice || choice=0
  case "$choice" in
    1)
      clear; echo "【体检】netdev doctor"; echo
      "$NETDEV" doctor
      echo; printf "${C_D}提示：上面每一项都要是 ✔；有 ! 的把内容发给我。${C_X}\n"
      pause
      ;;
    2)
      clear; echo "【设备与会话】"; echo
      "$NETDEV" list
      echo; "$NETDEV" conn list
      echo; "$NETDEV" screen-ls
      echo; printf "${C_D}接入请在网页终端页点「🔌 串口接入 / SSH / Telnet」，更直观。${C_X}\n"
      pause
      ;;
    3)
      clear; echo "【备份设备配置】"; echo
      printf "  设备名（直接回车 = serial-huawei）："; read -r dev || dev=""
      dev="${dev:-serial-huawei}"
      printf "  标签/客户名（如 客户A-到货，可空）："; read -r tag || tag=""
      printf "  备注（可空）："; read -r note || note=""
      args=("$NETDEV" snap save "$dev")
      [ -n "$tag" ] && args+=(--tag "$tag")
      [ -n "$note" ] && args+=(--note "$note")
      echo
      "${args[@]}"
      echo; printf "${C_D}存好的快照编号见上面输出；桌面 workbuddy 里也有一份副本。${C_X}\n"
      pause
      ;;
    4)
      clear; echo "【恢复配置】"; echo
      printf "  设备名（直接回车 = serial-huawei）："; read -r dev || dev=""
      dev="${dev:-serial-huawei}"
      echo
      "$NETDEV" snap list "$dev" || true
      printf "\n  用哪份快照？（填编号如 4，回车 = 最新）："; read -r ref || ref=""
      echo
      printf "${C_B}第一步：看差异（不会动设备）${C_X}\n"
      if [ -n "$ref" ]; then "$NETDEV" snap restore "$dev" --from "$ref"; else "$NETDEV" snap restore "$dev"; fi
      printf "\n  ${C_Y}确认要执行恢复吗？输入 RESTORE 才执行，其它任意键取消：${C_X}"
      read -r ok || ok=""
      if [ "$ok" = "RESTORE" ]; then
        if [ -n "$ref" ]; then "$NETDEV" snap restore "$dev" --from "$ref" --apply --yes; else "$NETDEV" snap restore "$dev" --apply --yes; fi
        echo; printf "${C_D}别忘了落盘：netdev save $dev（不确定就先别 save）${C_X}\n"
      else
        printf "${C_D}已取消，什么都没做。${C_X}\n"
      fi
      pause
      ;;
    5)
      clear; echo "【快照管理】"; echo
      "$NETDEV" snap list
      echo; "$NETDEV" snap trash
      printf "\n  ${C_Y}要删除哪些快照？（填编号，多个用空格，回车取消）：${C_X}"
      read -r refs || refs=""
      if [ -n "$refs" ]; then
        # shellcheck disable=SC2086
        "$NETDEV" snap rm $refs
        printf "\n  ${C_Y}确认删除（进回收区，可取回）？输入 YES：${C_X}"
        read -r ok || ok=""
        if [ "$ok" = "YES" ]; then "$NETDEV" snap rm $refs --yes; else printf "${C_D}已取消。${C_X}\n"; fi
      fi
      echo; printf "${C_D}取回：netdev snap unrm ｜ 彻底删：netdev snap purge --yes${C_X}\n"
      pause
      ;;
    6)
      clear; echo "【打开网页】"; echo
      "$NETDEV" ui open
      pause
      ;;
    7)
      clear; echo "【一键打包配置】"; echo
      TS=$(date +%Y%m%d_%H%M%S)
      OUT="$HOME/Desktop/workbuddy/${TS}_netdev配置打包.tar.gz"
      TMP=$(mktemp -d)
      cp -R "$ROOT/config" "$ROOT/backups" "$ROOT/README-从这里开始.txt" "$TMP/" 2>/dev/null
      if [ -f "$HOME/.pi/commands.json" ]; then mkdir -p "$TMP/.pi"; cp "$HOME/.pi/commands.json" "$TMP/.pi/"; fi
      tar -czf "$OUT" -C "$TMP" .
      rm -rf "$TMP"
      if [ -f "$OUT" ]; then
        ls -l "$OUT" | awk '{print "  ✔ 已打包：" $9 "  (" $5 " B)"}'
        echo; printf "${C_D}这份压缩包 = 你的全部配置+备份，放到别处（U盘/网盘）就更安全。${C_X}\n"
      else
        echo "  ✖ 打包失败"
      fi
      pause
      ;;
    8)
      clear; echo "【修复网页命令栏】"; echo
      "$NETDEV" web check
      echo; "$NETDEV" web repair
      echo; printf "${C_D}回到网页刷新一下即可（F5）。${C_X}\n"
      pause
      ;;
    9)
      clear; echo "【网页服务】"; echo
      "$NETDEV" ui status
      echo
      printf "  1) 启动 / 确保在跑   2) 重启   3) 停止   4) 看日志（末 40 行）   回车=返回\n"
      printf "\n  ${C_Y}你的选择：${C_X}"; read -r sc || sc=""
      echo
      case "$sc" in
        1) "$NETDEV" ui start ;;
        2) "$NETDEV" ui restart ;;
        3) "$NETDEV" ui stop ;;
        4) "$NETDEV" ui log -n 40 ;;
        *) printf "${C_D}没做任何变更。${C_X}\n" ;;
      esac
      svc_refresh
      pause
      ;;
    0|q|Q)
      clear; echo "已退出。（网页服务是后台守护，退出菜单不影响它）"; exit 0
      ;;
    *)
      echo "  没这个选项，请重新输入"; sleep 1
      ;;
  esac
done
