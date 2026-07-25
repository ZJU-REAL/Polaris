/* macOS 打包后补一个 ad-hoc 签名。

   为什么必须有：Electron 的预编译二进制自带 ad-hoc 签名，但 electron-builder
   改完包（换图标、塞 app.asar、放 extraResources）之后那个签名就失效了——
   实测 `codesign -v` 报 "code has no resources but signature indicates they must
   be present"。Apple Silicon 的内核**拒绝执行完全无有效签名的二进制**，用户会
   看到「已损坏，无法打开」，而且这不是 quarantine 属性，xattr 删不掉。

   `identity: null` 只是「不用 Developer ID 签名」，它不会替你做 ad-hoc 签名。
   所以这里显式补一次：--sign - 就是 ad-hoc，--deep 覆盖内部的 framework 与
   helper。这不能替代公证（notarization），只是让未签名分发能真的启动。 */

const { execFileSync } = require('node:child_process');
const path = require('node:path');

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const appPath = path.join(
    context.appOutDir,
    `${context.packager.appInfo.productFilename}.app`,
  );

  try {
    execFileSync('codesign', ['--force', '--deep', '--sign', '-', appPath], {
      stdio: 'inherit',
    });
    // 立刻验一遍：签名坏了要在打包阶段就炸，而不是等用户双击才发现
    execFileSync('codesign', ['--verify', '--deep', appPath], { stdio: 'inherit' });
    console.log(`  • ad-hoc signed and verified  ${appPath}`);
  } catch (err) {
    throw new Error(
      `ad-hoc 签名失败，产物在 Apple Silicon 上会无法启动：${err.message}`,
    );
  }
};
