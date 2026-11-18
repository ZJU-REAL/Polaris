/* ============================================================
   内置插件注册表（#699，D1/D3）。

   打包态（esbuild + asar）里动态 import 内置插件是 R1 问题的根源，
   所以树里的内置插件全部走 loader.builtins 查表：配置树条目用
   cordis:<键> specifier，EntryTree.import 命中前缀直接查表返回，零动态
   import。

   这张表现在是空的，而且是故意的：
   - storage 不入表（D3）：loader 树本身就持久化在 storage 的
     SqliteConfigTreeStore 里，storage 是先于 loader 直挂的地基。
   - legacy-engine 不入表：引擎参数每次启动现算（venv 的绝对路径、端口、
     密钥文件），进了树就会被任何一次树写入原样落库——下次启动树拿着
     上个版本的路径先把引擎拉起来，而且用户能在设置里把它关掉。它和
     storage 一样由宿主直挂（见 host.ts 的 mountEngine）。
   配置树因此只装用户自己装的插件；旧版本种在树里的内置条目由
   host.ts 的 dropUnknownBuiltins 在装载前清掉。
   本文件必须保持 electron-free（tests/electron-free.test.ts 强制）。
   ============================================================ */

import { Group, type Loader } from '@deepseek-ai/cordis-plugin-loader'

/** 键 = cordis: 后面的 specifier 尾巴。 */
export const BUILTIN_PLUGINS: Record<string, unknown> = {}

/** 组条目的 specifier 尾巴（cordis:group）：loader 自带的载体插件。 */
export const GROUP_BUILTIN = 'group'

/** 把内置插件表灌进 loader.builtins。装 SqliteTree 之前调用。 */
export function registerBuiltins(loader: Loader): void {
  Object.assign(loader.builtins, BUILTIN_PLUGINS)
  // 组插件也要可导入：配置树里的组条目（children）序列化成
  // name=cordis:group 的行，Entry 初始化照样按 name 查表拿插件。
  // Group 是 loader 包自带的载体插件，不算 Polaris 内置，故不进上表。
  loader.builtins[GROUP_BUILTIN] = Group
}
