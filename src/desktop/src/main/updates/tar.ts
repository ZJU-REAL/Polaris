/* ============================================================
   最小 tar.gz 解包器。

   为什么自己写：这个包一直是零运行时依赖（见 store.ts 里不引 electron-store 的
   理由），而 Node 没有内置 tar。tar 格式本身很简单——512 字节头 + 512 对齐的
   数据块——真正需要小心的只有安全边界，那部分不该外包给一个不看源码的依赖。

   解包对象是从网络下载的更新包，所以按不可信输入对待：
   - 逐条目校验归一化后的路径仍在目标目录内（zip-slip）
   - 只接受普通文件与目录，跳过软链/硬链/设备文件——软链是穿越的另一条路
   - 限制单文件与总解包体积，避免解压炸弹
   ============================================================ */

import { gunzipSync } from 'node:zlib';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, normalize, sep } from 'node:path';

const BLOCK = 512;
const MAX_FILE_BYTES = 64 * 1024 * 1024;
const MAX_TOTAL_BYTES = 256 * 1024 * 1024;

function readString(buf: Buffer, offset: number, length: number): string {
  const raw = buf.subarray(offset, offset + length);
  const end = raw.indexOf(0);
  return raw.subarray(0, end === -1 ? raw.length : end).toString('utf8');
}

/** tar 的 size 字段是 8 进制 ASCII。 */
function readOctal(buf: Buffer, offset: number, length: number): number {
  const text = readString(buf, offset, length).trim();
  return text ? parseInt(text, 8) || 0 : 0;
}

/**
 * 把 tar.gz 解到 destination。返回写出的相对路径列表。
 * 任何越界或不支持的条目都直接抛错——更新包宁可整体失败，也不要半个。
 */
export function extractTarGz(archive: Buffer, destination: string): string[] {
  const tar = gunzipSync(archive);
  const root = normalize(destination);
  const written: string[] = [];
  let total = 0;

  for (let offset = 0; offset + BLOCK <= tar.length; ) {
    const header = tar.subarray(offset, offset + BLOCK);
    // 连续两个空块 = 归档结束
    if (header.every((b) => b === 0)) break;

    const name = readString(header, 0, 100);
    const size = readOctal(header, 124, 12);
    const typeflag = readString(header, 156, 1) || '0';
    const prefix = readString(header, 345, 155);
    const raw = prefix ? `${prefix}/${name}` : name;
    offset += BLOCK;

    if (!raw) throw new Error('tar: empty entry name');
    // CI 用 `tar -C dist .` 打包，条目名形如 ./index.html、./pdfjs/，且**第一条就是
    // "./" 目标目录自身**。去掉 ./ 前缀与结尾斜杠得到规范相对路径；归一化后为空
    // 说明这条就是根目录，跳过即可——不是错误。
    const rel = raw.replace(/^\.\/+/, '').replace(/\/+$/, '');
    if (!rel) {
      offset += Math.ceil(size / BLOCK) * BLOCK;
      continue;
    }
    if (size < 0 || size > MAX_FILE_BYTES) throw new Error(`tar: entry too large: ${rel}`);
    total += size;
    if (total > MAX_TOTAL_BYTES) throw new Error('tar: archive exceeds size limit');

    const target = normalize(join(root, rel));
    if (target !== root && !target.startsWith(root + sep)) {
      throw new Error(`tar: entry escapes destination: ${rel}`);
    }

    if (typeflag === '5') {
      mkdirSync(target, { recursive: true });
    } else if (typeflag === '0' || typeflag === '') {
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, tar.subarray(offset, offset + size));
      written.push(rel);
    } else {
      // 1=硬链 2=软链 3/4=设备 6=fifo：更新包里不该出现，出现即视为异常
      throw new Error(`tar: unsupported entry type '${typeflag}': ${rel}`);
    }

    offset += Math.ceil(size / BLOCK) * BLOCK;
  }

  return written;
}
