# netdev bash 补全 —— 自包含，不依赖 bash-completion 包；兼容 macOS 自带 bash 3.2
# 由 pi 于 2026-09-17 生成
# 用法：在 ~/.bashrc 里 source 本文件（已自动接好），或临时 `source ~/.bash_completion.d/netdev`

_netdev_devices() {
  local conf="$HOME/netops/devices.toml" names
  [ -f "$conf" ] || return 0
  names=$(sed -n 's/^[[:space:]]*name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$conf")
  COMPREPLY=($(compgen -W "$names" -- "$cur"))
}

_netdev_backups() {
  local dir="$HOME/netops/backups"
  [ -d "$dir" ] || return 0
  COMPREPLY=($(compgen -W "$(ls -1 "$dir"/*.cfg 2>/dev/null)" -- "$cur"))
}

_netdev_changes() {
  local dir="$HOME/netops/changes"
  [ -d "$dir" ] || return 0
  COMPREPLY=($(compgen -W "$(ls -1 "$dir"/*.txt 2>/dev/null | grep -v '/README.txt$')" -- "$cur"))
}

_netdev_common_cmds() {
  local cmds="display\ version display\ clock display\ device display\ esn \
display\ current-configuration display\ saved-configuration \
display\ interface\ brief display\ ip\ interface\ brief \
display\ ip\ routing-table display\ vlan display\ port\ vlan \
display\ acl\ 2000 display\ nat\ outbound display\ nat\ session\ all \
display\ arp\ all display\ users display\ ssh\ server\ status \
display\ cpu-usage display\ memory-usage display\ logbuffer \
display\ reboot-info dir"
  COMPREPLY=($(compgen -W "$cmds" -- "$cur"))
}

_netdev_bash() {
  local cur prev sub
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  sub="${COMP_WORDS[1]}"

  # 第一个参数：子命令
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "list run apply save backup diff ping shell watch cmds hint serial-discover onboard selftest mcp" -- "$cur"))
    return 0
  fi

  case "$sub" in
    run|hint)
      if [ "$COMP_CWORD" -eq 2 ]; then
        _netdev_devices
      else
        _netdev_common_cmds
      fi
      ;;
    apply)
      case "$prev" in
        --file) _netdev_changes ;;
        *)
          if [ "$COMP_CWORD" -eq 2 ]; then
            _netdev_devices
          else
            COMPREPLY=($(compgen -W "--cmd --file --verify --yes --no-save --rollback" -- "$cur"))
          fi
          ;;
      esac
      ;;
    save|backup|shell|onboard|ping)
      if [ "$COMP_CWORD" -eq 2 ]; then
        _netdev_devices
      elif [ "$sub" = ping ] && [ "$COMP_CWORD" -eq 3 ]; then
        COMPREPLY=($(compgen -W "8.8.8.8 114.114.114.114 223.5.5.5 192.168.1.1" -- "$cur"))
      elif [ "$sub" = ping ]; then
        COMPREPLY=($(compgen -W "--source --count" -- "$cur"))
      fi
      ;;
    watch)
      if [ "$COMP_CWORD" -eq 2 ]; then
        _netdev_devices
      else
        COMPREPLY=($(compgen -W "--lines --no-follow" -- "$cur"))
      fi
      ;;
    diff)
      _netdev_backups
      ;;
    cmds)
      [ "$COMP_CWORD" -eq 2 ] && COMPREPLY=($(compgen -W "huawei h3c ruijie cisco 华为 华三 锐捷 思科" -- "$cur"))
      ;;
  esac
  return 0
}

complete -o default -F _netdev_bash netdev
