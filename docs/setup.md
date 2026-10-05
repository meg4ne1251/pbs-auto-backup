# 導入と実機確認

この実装は PVE/PBS とも Python 3 の標準ライブラリだけを使う。最初に [設計書](design.md) と以下の実機確認を済ませ、設定例の値を置き換える。PVE の既存バックアップジョブや PBS の保守ジョブは変更しない。

## 導入前に確認する値

- PVE/PBS のバージョン、PVE のタイムゾーン。cron の 5:50 と 6:30、および既存バックアップの 6:00 が同じローカル時刻であること。
- PBS の IP/ホスト名、停止状態から起動できる NIC の MAC、PVE から届く WOL ブロードキャストアドレス。`ping` と SSH の接続先は同じ PBS にする。
- PBS の datastore 名・実際のパス・そのパスを含む ZFS dataset と pool。datastore はその dataset のマウント内にあり、別のマウントを挟まない構成とする。
- 月次保守の実行内容。初版は PBS の実行中タスクと、設定した pool の scrub/resilver 状態を監視する。別 pool、TRIM、SMART 長時間テスト、外部スクリプト等も停止保留の対象なら、その状態取得を追加するまで cron を有効にしない。
- `proxmox-backup-manager task list --limit 1000 --output-format json` が root 実行で全ユーザーの実行中タスクを配列として返すこと。1000 件に達した場合は安全のため停止しない。PBS 版によってコマンドや JSON 形式が違う場合は導入前に調整する。
- `zpool status <pool>` の `scan:` 行が `pbs_agent.py` で認識できる形式であること。未知の形式は停止保留となる。
- 起動上限 10 分、WOL 再送 2 分、停止確認 5 分、停止再試行 1 回、引き継ぎ待ち 10 分の値が実機に合うこと。停止確認の上限を変えると、翌朝の停止要求保留の開始時刻も変わる。

## PBS 側

1. `pbs_agent.py` を `/opt/pbs-auto-backup/pbs_agent.py` に root 所有で配置し、`config/pbs.example.json` を `/etc/pbs-auto-backup/pbs.json` にコピーして実値を設定する。ファイルと親ディレクトリを root 所有にし、一般ユーザーが変更できないようにする。
2. root で `python3 /opt/pbs-auto-backup/pbs_agent.py --config /etc/pbs-auto-backup/pbs.json ready` と `... idle` を実行し、JSON の結果と実際の状態を照合する。scrub 実行中・一時停止中も確認する。
3. PVE 専用の SSH 公開鍵を PBS の root の `authorized_keys` に登録する。鍵の行頭に次の制約を付ける。鍵そのものは末尾に続ける。

   ```text
   restrict,command="/usr/bin/python3 /opt/pbs-auto-backup/pbs_agent.py --config /etc/pbs-auto-backup/pbs.json" ssh-ed25519 AAAA... pve-pbs-auto-backup
   ```

   `restrict` と強制コマンドにより、この鍵では `ready`、`idle`、`shutdown` 以外を受け付けない。スクリプトと設定を root だけが変更できることを確認する。root SSH ログインを禁止している環境では、限定 sudo 権限を持つ専用ユーザー向けの構成を別途決める。

## PVE 側

1. `pve_controller.py` を `/opt/pbs-auto-backup/pve_controller.py` に配置する。`config/pve.example.json` を `/etc/pbs-auto-backup/pve.json` にコピーし、実値を設定する。秘密鍵・設定・状態ディレクトリは root のみ読み書き可能にする。状態ディレクトリは再起動後も残る場所を指定する。
2. PBS のホスト鍵を検証済みの方法で `/root/.ssh/known_hosts` に登録する。自動承認や `StrictHostKeyChecking=no` は使わない。
3. PVE root から `ssh -i /root/.ssh/pbs-auto-backup -o BatchMode=yes root@<PBS> ready` と `... idle` を実行して JSON が得られることを確認する。`... shutdown` は PBS の停止要求になるため、実機試験でのみ実行する。
4. 停止中の PBS に対して `python3 /opt/pbs-auto-backup/pve_controller.py start --config /etc/pbs-auto-backup/pve.json` を手動実行し、起動後に datastore まで使えることを確認する。
5. バックアップ・scrub 実行中、SSH 失敗時、状態解析失敗時、通常日の上限、月次の引き継ぎ、翌朝 5:40〜6:30 の停止保留を検証する。停止要求の拒否・SSH 切断時に保守状態が残り、停止確認中に起動処理が割り込まないことも確認する。その後、PVE root の crontab に次を登録する。

   ```cron
   50 5 * * * /usr/bin/python3 /opt/pbs-auto-backup/pve_controller.py start --config /etc/pbs-auto-backup/pve.json >>/var/log/pbs-auto-backup.log 2>&1
   30 6 * * * /usr/bin/python3 /opt/pbs-auto-backup/pve_controller.py stop --config /etc/pbs-auto-backup/pve.json >>/var/log/pbs-auto-backup.log 2>&1
   ```

ログは PVE 側で logrotate 等を使って保管期間を設定する。監視は最長 24 時間動き、翌日の cron は前回の監視ロック解放を最大 10 分待つ。ロック待ち上限に達した場合はログにエラーが残り、自動で代わりの監視は始まらないため、原因を調査する。

## 障害時

- 起動失敗時は WOL 到達性、BIOS/NIC 設定、SSH ホスト鍵、PBS サービス、pool と datastore のマウントを確認する。PVE のバックアップジョブをこのスクリプトが再実行することはない。
- 状態取得失敗、認識できない出力、停止要求失敗時は PBS を稼働させたままログに理由を残す。PBS のタスク一覧と ZFS の scan 状態を手動で確認する。
- `maintenance.json` は、月次保守監視を翌日以降へ引き継ぐための状態。監視が上限終了したときや、停止要求の結果が不明なまま PBS が到達不能になったときも消さない。手動で削除する場合は、PBS の停止状態と保守処理が終了したことを先に確認する。
- 停止要求後の ping 連続不応答は到達不能の確認であり、電源 OFF の証明ではない。必要なら BMC 等で実際の電源状態を確認する。

## 実機を使わない検証

`python3 -m unittest discover -s tests -v` で、状態解析失敗時の停止保留、実行中タスクの検知、保守状態の保存、停止要求の拒否・通信断、起動と停止のロックを検証できる。

コマンド仕様の参照: [PBS の task/datastore コマンド](https://pbs.proxmox.com/docs/proxmox-backup-manager/man1.html)、[OpenZFS の scrub と一時停止](https://openzfs.github.io/openzfs-docs/man/v2.2/8/zpool-scrub.8.html)。実機のバージョンで出力と動作を照合すること。
