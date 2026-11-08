# OBSBOT Meet 2 on Linux

Researched 2026-11-07, measured 2026-11-08. Notes on running the OBSBOT Meet 2 webcam, the camera `Capture` reads, on the layout's Linux host (an Intel Core i7 x86 machine). Every claim has a numbered source; the list is at the end. "Unverified" means I found no primary source for it. Evidence that only exists for a sibling model (Meet, Meet 4K, Meet SE, Tiny 2, Tail Air, ...) says so.

Much of the detailed Meet 2 data comes from one community USB capture of one camera on firmware 4.4.6.1 [S12][S13]. OBSBOT has since released firmware 4.4.7.1 [S2], and that capture was not repeated on it.

Section 9 records measurements on our own camera, on macOS, before and after updating it to 4.4.7.1. Where they settle a point marked unverified above, the TL;DR says so.

## TL;DR

1. **Basic video:** It is a plain UVC device (USB ID `3564:fefb`) handled by the stock `uvcvideo` driver. Mainline has no device-specific quirk. OBSBOT documents MJPEG up to 3840x2160@30 and 1920x1080/1280x720@60, plus uncompressed YUV at 640x480 and 640x360 only. It lists no H.264. No published `v4l2-ctl --list-formats-ext` dump exists.
2. **Controls:** The standard V4L2 controls cover brightness, contrast, hue, saturation, sharpness, gain, anti-flicker, white balance (auto and Kelvin), exposure (auto and absolute), focus (auto and absolute), zoom, and pan/tilt (digital). AI, HDR, FOV, face-AE/AF and gestures go through a vendor extension unit, GUID `{9a1e7291-6843-4683-6d92-39bc7906ee49}`. You can drive it with OBSBOT's Linux SDK (`libdev.so`) via `obsbot-camera-control`, or with SDK-free community tools.
3. **Fixing exposure/WB/focus:** Yes. Turn off `auto_exposure`, `white_balance_automatic` and `focus_automatic_continuous`, then set the absolute values. No source shows that these persist across power-cycle or replug, but on our camera they do (section 9). A udev-triggered systemd unit that re-applies them is optional.
4. **AI off:** Yes. Set AI mode to "none" (extension unit selector 6, field `0x16` = `00 00`) and send the gesture-off command (selector 2). On our camera, AI off, face AE off and the gesture switches set in OBSBOT Center all survive a replug, and the gesture switches show in status bytes 38–39 (section 9).
5. **Streaming:** Pass MJPEG through without re-encoding (go2rtc `v4l2:` source, or ffmpeg `-c:v copy`). For WebRTC/MSE/HLS in a browser you need H.264, so transcode with VA-API (`h264_vaapi`) on the i7's iGPU, or with libx264. go2rtc and MediaMTX both work.
6. **Firmware:** Only through OBSBOT Center, which runs on Windows and macOS only. The Linux SDK contains internal upgrade code but exposes no upgrade API. A Windows VM with USB passthrough is the community workaround, documented only for the Tiny 2. Our camera was updated from 4.4.6.6 to 4.4.7.1 with OBSBOT Center on a Mac (section 9).
7. **48 MP stills:** Not reachable from Linux. "48M" is the sensor's effective pixel count, and the largest documented output is 3840x2160 (about 8.3 MP). OBSBOT publishes no Meet 2 photo resolution or photo API. `uvcvideo` skips UVC still-image descriptors entirely. The practical maximum is one 3840x2160 MJPEG frame.
8. **OBS Studio:** The "Video Capture Device (V4L2)" source handles MJPEG and YUYV, and lists the standard UVC controls under "Camera Controls". OBS writes a control only when you change it in the dialog; nothing in the source re-applies controls when the source starts. AI and gestures need an external tool. OBSBOT has no OBS plugin for Linux.

---

## 1. Basic UVC video

**Plug and play.** The OBSBOT manual (retailer-distributed copy) says the Meet 2 "is connected to the computer via USB, plug and play". It lists only Windows 10+ and macOS 11+ as supported systems [S3]. OBSBOT's spec page also names only Windows and macOS [S1]. There is no official Linux support statement.

**USB IDs and driver.** The Meet 2 enumerates as `3564:fefb`, vendor Remo Tech (OBSBOT's parent), USB class 0e (video), and binds to `uvcvideo` (`CONFIG_USB_VIDEO_CLASS`). linux-hardware.org has six probes, two marked "works" (Arch, CachyOS) and four "detected" (Ubuntu 24.04, Bazzite, OpenMandriva, Nobara) [S8]. A community capture confirms `3564:fefb` on Debian with `uvcvideo` and `/dev/video0` [S12]. Vendor ID 3564 is not in the linux-usb.org `usb.ids` list (checked 2026-11-07), so `lsusb` may show no vendor name [S27].

**Kernel quirks.** Mainline `drivers/media/usb/uvc/uvc_driver.c` (master, 7.4-rc1) has no entry for vendor `0x3564`, so the camera runs with default UVC handling [S20]. I found no reports of Meet 2 needing a quirk (`quirks=` module parameter); that is unverified rather than confirmed absent.

**Bandwidth.** The data link is USB 2.0 over a USB-C connector [S1].

**Formats, resolutions and frame rates.** From OBSBOT's spec page [S1]:

| Format | Resolution | Frame rates |
|---|---|---|
| MJPEG | 3840x2160 | 30, 25, 24, 20, 15 |
| MJPEG | 1920x1080 | 60, 50, 30, 25, 24, 20, 15 |
| MJPEG | 1280x720 | 60, 50, 30, 25, 24, 20, 15 |
| YUV (uncompressed) | 640x480, 640x360 | 30, 25, 24, 20, 15 |

The FAQ says "lower settings are available" without listing them [S4]. The spec page lists no H.264 or H.265 stream [S1].

A real `v4l2-ctl --list-formats-ext` dump for the Meet 2 has not been published that I could find (unverified). Based on the spec table, I would expect two formats: `MJPG` with the sizes above, and a 4:2:2 YUV format (probably `YUYV`, unverified) with only the two small sizes. The status block reports `fps` = 60 at offset 31 [S12].

To check on the real camera, use these commands (option syntax from the v4l-utils source [S26]):

```sh
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
v4l2-ctl -d /dev/video0 --list-ctrls-menus
```

**UVC topology** (from `lsusb -v`, community capture [S12]): camera terminal ID 1, processing unit ID 3, extension unit ID 2, output terminal ID 7.

## 2. Camera controls from Linux

### Standard UVC controls (no vendor software needed)

The capture lists the descriptor control bitmaps: camera terminal `bmControls 0x00023e3a`, processing unit `bmControls 0x0000f7df` [S12]. I decoded the bits against the kernel's own bit-to-control table in `uvc_ctrl.c` [S21]:

- **Camera terminal (bits 1, 3, 4, 5, 9, 10, 11, 12, 13, 17):** AE mode, exposure time absolute and relative, focus absolute, zoom absolute and relative, pan/tilt absolute and relative, roll absolute, and focus auto. Bit 2 (AE priority) is not set, so expect no `exposure_dynamic_framerate` control.
- **Processing unit (bits 0–4, 6–10, 12–15):** brightness, contrast, hue, saturation, sharpness, white-balance temperature and component, backlight compensation, gain, power-line frequency, white-balance temperature auto and component auto, digital multiplier and its limit. There is no gamma (bit 5) and no hue auto (bit 11).

Which V4L2 controls the kernel creates from these bits depends on its mapping table. Some advertised controls (for example roll and the relative variants) may show up differently or not at all; unverified.

These V4L2 control names were seen in use on a Meet 2 in the capture, with observed values or ranges [S12][S13]:

| V4L2 control | Observed range / notes |
|---|---|
| `brightness`, `contrast`, `saturation`, `hue`, `sharpness` | 0–100 (not 0–255) |
| `gain` | 1–64 (the SDK's "ISO") |
| `power_line_frequency` | 0–2 |
| `backlight_compensation` | read as 12; the SDK uses it as EV bias |
| `white_balance_automatic`, `white_balance_temperature` | 2000–10000 K |
| `auto_exposure`, `exposure_time_absolute` | 100 µs units; seen values 16, 167, 330 |
| `focus_automatic_continuous`, `focus_absolute` | 0–100 |
| `pan_absolute`, `tilt_absolute` | ±648000 (V4L2 arc-second units); digital pan/tilt inside the crop |
| `zoom_absolute` | present; independent of the SDK's zoom command (see below) |

Control semantics follow the V4L2 camera control reference: `auto_exposure` 0 = auto, 1 = manual [S22]. That matches the capture: the SDK writes `auto_exposure=0` for auto and `auto_exposure=1` before setting `exposure_time_absolute` [S12].

**Zoom caveat:** the SDK's zoom is a vendor command. The camera also exposes V4L2 `zoom_absolute`, but it stayed at 4 through a sweep of SDK zoom commands, so the two look independent. The V4L2 zoom path was not tested [S12].

### Vendor extension unit

There is one extension unit: ID 2, GUID `{9a1e7291-6843-4683-6d92-39bc7906ee49}`, 7 selectors of 60 bytes each [S11][S12]. Linux reaches it through the standard `UVCIOC_CTRL_QUERY` ioctl of `uvcvideo` [S23]. Two selectors matter [S12]:

- **Selector 6** returns a 60-byte status block on GET_CUR and takes a single field write `[field id][length][value]` on SET_CUR. Verified fields: HDR `0x01`, face-AE `0x03`, FOV `0x04` (0/1/2 = 86°/78°/65°), horizontal flip `0x14`, AI mode `0x16` (2 bytes: mode, sub-mode), mic auto-gain `0x17`, LED `0x18`.
- **Selector 2** carries framed, checksummed commands. They cover face auto-focus, zoom, auto-zoom, tracking speed, AE lock, and gesture control (dest `0x04`, code `c4 ff`, body `05 01` on / `05 00` off).

The AI mode values are 0 off, 1 group, 2 human (sub-modes 0–4), 3 hand, 4 whiteboard, and 5 desk [S12]. They match the SDK header's `AiWorkModeType` enum [S10].

### Software that drives the controls

- **Any V4L2 tool** (`v4l2-ctl`, `cameractrls`, `guvcview`, the OBS V4L2 source) can set the standard controls. One linux-hardware.org prober says the camera "works nicely, with cameractrls or similar software" (anecdotal) [S8]. `cameractrls` has vendor extensions only for Logitech, Kiyo Pro, Dell and Anker cameras, none for OBSBOT, so for the Meet 2 it shows standard controls only [S18].
- **OBSBOT SDK.** OBSBOT's SDK page lists Linux (Ubuntu) next to Windows and macOS and names the Meet 2 as compatible. You get it by applying through a form, and it arrives by email [S5]. A copy (`libdev.so`, x86_64, headers, no licence file) ships inside `obsbot-camera-control` [S9][S11]. The header enumerates `ObsbotProdMeet2` [S10]. It is a closed binary.
- **`aaronsb/obsbot-camera-control`** (Qt6 GUI plus `obsbot-cli`, MIT licence, uses the bundled SDK) lists the Meet 2 as a primary, "Full" support target [S9]. Its CLI applies `~/.config/obsbot-control/settings.conf` to the camera and exits [S9][S15]. Open Meet 2 bugs exist. In issue #33, continuous AF in the GUI only works with auto-framing on; `v4l2-ctl --set-ctrl=focus_automatic_continuous=1` works around it, and the owner says the underlying GUI bug remains [S16]. Issue #56 (fixed by a high-DPI commit) is a snapshot/v4l2loopback cropping bug [S17].
- **SDK-free tools** (community, experimental): `xu_raw.py` from the `strips` fork reads the status block and writes selector-6 fields and selector-2 frames. Its selector-2 checksum constants were measured on a Meet 2 on firmware 4.4.6.1 [S12][S14]. `obsbot-xu-ctl` from the `laktosterror` fork is a fuller CLI, but its author has verified it only on a **Meet SE**, not the Meet 2 [S19]. Upstream issue #39 tracks replacing the closed SDK with an open protocol library [S11].
- **SDK quirks** observed on the Meet 2 [S12]: getters return success when the camera rejected the query; selector-2 setters are fire-and-forget, so a return code of 0 does not mean the change was applied; white-balance presets write `white_balance_temperature=0` instead of a Kelvin value; and `aiGetAiStatusR` fails.

## 3. Fixing exposure, white balance and focus

**Can they be set to manual?** Yes, through standard V4L2 controls. The SDK itself uses exactly these writes on the Meet 2 [S12]:

```sh
DEV=/dev/video0   # better: the stable /dev/v4l/by-id/... symlink, see below
# order: switch auto off first, then write the value
v4l2-ctl -d "$DEV" --set-ctrl=auto_exposure=1               # 1 = manual [S22]
v4l2-ctl -d "$DEV" --set-ctrl=exposure_time_absolute=167    # 100 us units -> 16.7 ms
v4l2-ctl -d "$DEV" --set-ctrl=gain=5
v4l2-ctl -d "$DEV" --set-ctrl=white_balance_automatic=0
v4l2-ctl -d "$DEV" --set-ctrl=white_balance_temperature=4800
v4l2-ctl -d "$DEV" --set-ctrl=focus_automatic_continuous=0
v4l2-ctl -d "$DEV" --set-ctrl=focus_absolute=40
v4l2-ctl -d "$DEV" --set-ctrl=power_line_frequency=1        # 1 = 50 Hz (Switzerland) [S51]
```

The values are examples taken from the capture's defaults [S12]; tune them for the layout lighting. Set the mode before the value. The kernel itself notes that it does not order "Auto-Exposure Mode before Exposure Time" when it restores controls [S21].

Two vendor features can also move exposure and focus, and both sit outside V4L2: face-based AE (selector 6 field `0x03`) and face AF (selector 2) [S12]. For a fixed scene, turn both off:

```sh
python3 xu_raw.py --device "$DEV" set 0x03 00     # face AE off      [S14]
python3 xu_raw.py --device "$DEV" face-focus off  # face AF off      [S14]
python3 xu_raw.py --device "$DEV" hdr off         # HDR off (default is off per manual [S3])
```

These commands are tested on one Meet 2 on firmware 4.4.6.1 [S12]. They are not verified on firmware 4.4.7.1.

**Do the settings persist across reboot or replug?** Unverified. No source documents whether the Meet 2 keeps UVC control values in non-volatile memory. The kernel side is clear, though: `uvcvideo` re-writes modified controls only after a USB reset-resume (`uvc_ctrl_restore_values()`, called from `__uvc_resume()` when `reset` is set), not after a replug or a reboot [S20][S21]. The obsbot-camera-control project also treats settings as host-side. It stores them in `~/.config/obsbot-control/settings.conf` and re-applies them, with "systemd service for auto-apply on boot" on its roadmap [S9][S28]. Measured since: our camera keeps its UVC control values across a replug (section 9). Re-applying them when the camera appears guards against a firmware update resetting them.

**Re-applying at boot or replug.** udev can start a systemd unit when the device appears, via `TAG+="systemd"` and `ENV{SYSTEMD_WANTS}` [S24]. Sketch (untested on this camera):

```
# /etc/udev/rules.d/90-obsbot-meet2.rules
ACTION=="add", SUBSYSTEM=="video4linux", ATTRS{idVendor}=="3564", ATTRS{idProduct}=="fefb", \
  ATTR{index}=="0", TAG+="systemd", ENV{SYSTEMD_WANTS}+="obsbot-meet2-setup.service"
```

```ini
# /etc/systemd/system/obsbot-meet2-setup.service
[Unit]
Description=Apply fixed settings to OBSBOT Meet 2

[Service]
Type=oneshot
ExecStart=/usr/local/bin/obsbot-meet2-setup.sh
```

`obsbot-meet2-setup.sh` contains the `v4l2-ctl` and `xu_raw.py` calls above. Address the camera by its `/dev/v4l/by-id/usb-...-video-index0` symlink rather than `/dev/video0`; the exact name for the Meet 2 is unverified, so check `ls -l /dev/v4l/by-id/`. The OBSBOT SDK needs about 3.5 s after connect before it reports the camera [S19], so if the script uses `obsbot-cli` instead of raw tools, add a retry loop. Whether the camera resets any controls when a stream starts is unverified. If values drift, run the script again after the streaming service has opened the device.

## 4. Turning AI features off

**Auto-framing and tracking.** AI mode is selector 6, field `0x16`, with a 2-byte value (mode, sub-mode). `00 00` turns AI off. The SDK's `cameraSetAiModeU(AiWorkModeNone)` produces exactly that write on a Meet 2 [S12]. The SDK header documents `cameraSetAiModeU` as Tiny 2 only, but the capture shows it working on the Meet 2 [S10][S12]. Without the SDK:

```sh
python3 xu_raw.py --device "$DEV" set 0x16 0000    # AI mode: none   [S12][S14]
python3 xu_raw.py --device "$DEV" status           # check ai_mode == 0
```

The camera LED is purple while auto-framing is on and green in standard mode [S3], which gives a quick visual check.

Note that `obsbot-cli` disables "face tracking" with `cameraSetMediaModeU(MediaModeNormal)` [S15]. The header documents that call for the Meet and Meet 4K only [S10], and the Meet 2 capture did not exercise media modes [S12]. Whether that path works on the Meet 2 is unverified. Use the AI-mode write above, or the GUI's AI-mode control, which calls `cameraSetAiModeU` [S29].

**Gesture control.** OBSBOT added gesture control to the Meet 2 in a firmware update announced on 5 Nov 2024 ("selection and zoom") [S6], and the product page advertises gesture-triggered auto-framing and zoom [S7]. On Linux, the SDK's gesture switch is the selector-2 command dest `04`, code `c4 ff`, body `05 00` for off [S12]:

```sh
python3 xu_raw.py --device "$DEV" cmd 0a04c4ff 0500   # gestures off (no reply is read) [S12][S14]
```

With the raw command, the gesture state does not show in the status block, so you cannot read it back to confirm. `gesture_para` (bytes 38–39) did not change [S12]. Turning gestures off in OBSBOT Center does change byte 38 (section 9). The header's `gesture_para` bitfield, documented "for meet2, meetSE", has separate `gesture_auto_frame` and `gesture_zoom` bits [S10]; the community tools do not write them individually.

**Does it persist in the camera?** Unverified. The status block has a `boot_mode` byte (offset 32) that always equals `(ai_mode << 4) | sub_mode` after an AI-mode write [S12]. The header describes it as "boot mode: bit0~4->ai_sub_mode, bit5-8->ai_mode" [S10]. That suggests the camera stores the AI mode as its power-on mode, but nobody has tested a power-cycle. The header also has `cameraSetBootModeU` for setting the boot AI mode, documented for the Tiny 2 only [S10]. For gestures there is no evidence either way. Measured since: on our camera AI off and the gesture switches survive a replug (section 9).

## 5. Streaming pipeline

**Avoid re-encoding where you can.** The camera already delivers compressed MJPEG at every useful resolution. Uncompressed YUV is limited to 640x480 [S1]. go2rtc's docs summarise the trade-off: passing MJPEG or H.264 from the camera "won't cost you the CPU usage", while "transcoding RAW format to MJPEG or H264 - will cost you a significant CPU usage" [S30]. Browsers, however, play only H.264 (or H.265 in some cases) over WebRTC, MSE and HLS [S31]. So a browser-facing pipeline either keeps MJPEG end to end, or transcodes once to H.264.

**CPU note.** On the Core i7 host, decoding and re-encoding 1080p MJPEG in software is feasible but it is the expensive step. If the i7 has an Intel iGPU, VA-API offloads it. go2rtc's hardware notes state that Intel iGPUs have H.264 encode/decode since Sandy Bridge and MJPEG decode since Skylake, all via VA-API on Linux [S32]. Check with `vainfo` that `VAEntrypointEncSlice` for H.264 is listed. A plain MJPEG passthrough costs almost no CPU, whatever the encoder.

### Option A: go2rtc (recommended for a web app)

go2rtc has a native V4L2 source (since v1.9.9) that takes `input_format=mjpeg` [S30][S33]:

```yaml
# go2rtc.yaml
streams:
  meet2: v4l2:device?video=/dev/video0&input_format=mjpeg&video_size=1920x1080&framerate=30
  # optional H.264 for WebRTC/MSE, hardware-encoded on the Intel iGPU
  meet2_h264: ffmpeg:meet2#video=h264#hardware=vaapi
```

- MJPEG with no transcoding: `http://HOST:1984/api/stream.mjpeg?src=meet2` [S34]. The bundled viewer `stream.html?src=meet2&mode=mjpeg` also plays MJPEG over WebSocket [S35].
- WebRTC/MSE: `stream.html?src=meet2_h264`. The viewer picks WebRTC, then MSE, then HLS, then MJPEG [S35].
- `#hardware=vaapi` selects the VA-API encoder; go2rtc enables hardware decoding only when hardware encoding is available, and Intel falls back to software decode if needed [S32].
- One V4L2 device can be open in only one program at a time [S30]. go2rtc then becomes the single consumer, and everything else reads from it.

Using the `/dev/v4l/by-id/...` path in `video=` should work but is untested.

### Option B: MediaMTX (RTSP / WebRTC / HLS server)

MediaMTX's webcam recipe runs ffmpeg from `runOnInit` and publishes to itself over RTSP [S36]. Their example re-encodes with libx264. With MJPEG input and VA-API it becomes:

```yaml
# mediamtx.yml (adapted from [S36]; VA-API flags from the ffmpeg docs [S37][S38][S39])
paths:
  cam:
    runOnInit: >
      ffmpeg -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 -i /dev/video0
      -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va
      -vf format=nv12,hwupload -c:v h264_vaapi -b:v 4M -g 60
      -f rtsp rtsp://localhost:$RTSP_PORT/$MTX_PATH
    runOnInitRestart: yes
```

MediaMTX then serves the path over RTSP, WebRTC (port 8889) and HLS (port 8888) [S40]. The software fallback is MediaMTX's own line [S36]: `-c:v libx264 -pix_fmt yuv420p -preset ultrafast`.

### Option C: plain ffmpeg / GStreamer

ffmpeg's V4L2 input takes `-input_format mjpeg`, `-video_size` and `-framerate`; `-list_formats all` prints what the device offers [S37]:

```sh
ffmpeg -f v4l2 -list_formats all -i /dev/video0
# record MJPEG as-is, no re-encode
ffmpeg -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 -i /dev/video0 -c:v copy out.mkv
```

GStreamer's `v4l2src` exposes `image/jpeg` caps. Its documented decode example is `gst-launch-1.0 v4l2src ! jpegdec ! xvimagesink` [S41]. For an MJPEG-in-browser setup without any server software, go2rtc (option A) is simpler than hand-building a GStreamer HTTP pipeline.

## 6. Firmware updates

- OBSBOT's manual states that the Meet 2 "upgrades through the OBSBOT Center software" [S3]. OBSBOT's upgrade guide offers auto or manual upgrade, both inside OBSBOT Center, with Windows and macOS sections only [S25].
- OBSBOT Center (v2.0.15.186, 2026-09-23) is offered for Windows 10+ and macOS 11+ only. The latest Meet 2 firmware is **4.4.7.1** (2026-10-09, "Fixed known bugs") [S2].
- The Linux SDK's binary contains upgrade code: `dev-upgrade.cpp`, `DevUpgradePrivate::doUvcUpgrade`, and firmware packaging and bsdiff/LZMA libraries [S11]. Its public headers expose no upgrade function, and nobody has published a working Linux upgrade route (unverified).
- **Workaround:** a Windows VM with USB passthrough. The Tiny4Linux author (writing about the **Tiny 2**, not the Meet 2) says Wine runs OBSBOT Center but "is unable to pass through the camera's USB signal", so a VM, for example VirtualBox, "can provide a practical workaround" [S42]. Whether a Meet 2 upgrade survives the re-enumeration during flashing inside a VM is unverified. The camera's LED shows blue/yellow alternating while upgrading and slow red on failure [S3]. Do not unplug it during an upgrade [S3][S25].

## 7. Full-resolution stills (the "48 MP" claim)

**What the spec says.** OBSBOT's spec page lists "Effective Pixels: 48M" for the 1/2" sensor. Its only output formats are the video modes in section 1, topping out at 3840x2160 (about 8.3 MP) [S1]. The spec page, FAQ and manual give no photo or still resolution [S1][S3][S4]. The product page does not mention 48 MP photos either [S7]. So "48 MP" is a sensor figure, not a documented still-image output. How the 48 MP sensor is read out for 4K is not documented (unverified).

**How OBSBOT Center captures photos.** Unverified. I found no OBSBOT documentation of a Meet 2 photo or snapshot feature, its resolution, or its mechanism. The SDK's photo functions (`cameraSetTakePhotosR`, `cameraSetPhotoQualityR`, `cameraSetPhotoFormatR`) are documented for the **Tail Air** only. No Meet 2 photo command appears in the headers [S10]. The `obsbot-camera-control` "Snapshot" button simply saves the next preview frame [S43].

**Can Linux use UVC still capture?** Not through `uvcvideo`. The driver reads `bStillCaptureMethod` from the VS input header into `streaming->header` and never uses it again anywhere in the driver. When parsing formats it skips any `VS_STILL_IMAGE_FRAME` descriptor without recording it [S20]. V4L2 therefore offers no UVC method 2/3 still trigger. Whether the Meet 2 advertises still-image frame descriptors, or any frame larger than 3840x2160, is unknown: no full `lsusb -v` or `--list-formats-ext` dump has been published (unverified). The spec lists nothing near 8000x6000 [S1].

**Practical fallback.** Grab one 3840x2160 MJPEG frame. Discard a few frames first so AE/AWB can settle (untested on this camera):

```sh
# ffmpeg [S37]
ffmpeg -f v4l2 -input_format mjpeg -video_size 3840x2160 -framerate 30 -i /dev/video0 \
       -ss 2 -frames:v 1 -q:v 2 still.jpg
# v4l2-ctl: raw MJPEG frame as delivered by the camera [S26]
v4l2-ctl -d /dev/video0 --set-fmt-video=width=3840,height=2160,pixelformat=MJPG \
         --stream-mmap --stream-skip=30 --stream-count=1 --stream-to=still.jpg
```

If go2rtc already owns the device, take a JPEG from it instead: `http://HOST:1984/api/frame.jpeg?src=meet2` (the go2rtc `jpeg` output [S34]). The resolution is whatever the stream runs at.

## 8. OBS Studio on Linux

**Formats and resolutions.** The "Video Capture Device (V4L2)" source enumerates the device's formats with `VIDIOC_ENUM_FMT`. It accepts raw formats it can map, including `YUYV`, plus `MJPEG` and `H264`, which it decodes with libavcodec [S44][S45]. For the Meet 2 that means MJPEG at 3840x2160@30, 1920x1080@60 or 1280x720@60, or YUYV at 640x480/640x360 only [S1]. Over USB 2.0, high resolutions are only available as MJPEG [S1]. Pick "Motion-JPEG" in the source's Video Format list for anything above 640x480. OBS then software-decodes the MJPEG through libavcodec [S45]; the plugin code shows no hardware-decode path for it (unverified beyond reading the source). Anecdotally, OBS has defaulted to YUYV for some cameras and had to be switched manually [S46].

**UVC controls in OBS.** The source properties have a "Camera Controls" group. It is built by iterating `VIDIOC_QUERYCTRL` and adds a slider, checkbox or menu for every control that is not disabled, read-only or volatile [S47]. So exposure, white balance, focus, zoom and the other standard controls from section 2 appear there. Vendor-unit controls (AI, HDR, FOV, gestures) do not, because `uvcvideo` exposes them only through `UVCIOC_CTRL_QUERY` and not as V4L2 controls unless mapped [S23].

**Do they stick?** Partly. OBS saves the values in the scene collection, but it writes a control to the camera only from the property's modified callback (`v4l2_control_changed` → `VIDIOC_S_CTRL`) [S47]. `v4l2-input.c` contains no code that re-applies controls when the source starts [S44]. My reading of the source, unverified at runtime: after a camera replug or reboot, the camera runs on its own values until you touch the control in the dialog. Users on the OBS forum report the same symptom for webcams in general (anecdotal) [S46]. Use the udev/systemd unit from section 3 rather than relying on OBS.

**AI and tracking from OBS.** Not reachable from the V4L2 source. Use `obsbot-camera-control` next to OBS: it keeps the control channel separate from the video node and is designed to run "alongside" OBS [S9]. Or use the raw XU tools from sections 3 and 4. Its preview needs exclusive V4L2 access, so close the preview while OBS holds the camera [S9].

**OBSBOT plugin for OBS.** I found none for Linux. OBSBOT's own integrations are a Stream Deck plugin and OSC, both tied to OBSBOT Center on Windows and macOS [S2][S48]. Generic PTZ plugins such as `obs-ptz` speak VISCA/ONVIF, which a USB Meet 2 does not offer (unverified for the Meet 2) [S49].

**Known issues.** I found no Meet-2-specific issues in the obs-studio tracker (unverified absence). Issues seen with the Meet 2 alongside OBS are in `obsbot-camera-control`: issue #56 (fixed, high-DPI) cropped snapshot and v4l2loopback output [S17], and issue #57 reports the v4l2loopback image freezing on Wayland when the control window is minimised (model not stated; open) [S50].

## 9. Measured on our camera

Measured on 2026-11-08 with the layout's Meet 2 on a MacBook Pro (M2, macOS), firmware 4.4.6.6 and then 4.4.7.1. Not yet repeated on the Linux host.

**Firmware.** The camera shipped with 4.4.6.6 and was updated to 4.4.7.1 with OBSBOT Center on macOS. The selector-2 query dest `0d`, code `08 04` (flags `0x01`, no body) returns the version as a 4-byte body in reverse order: `06 06 04 04` before the update, `01 07 04 04` after. The query `0d 48 19` returns the same four bytes at body offset 4. USB `bcdDevice` reads `0x0510` on both versions, so it does not show the firmware. The 12-byte header checksum constant `0xFD9B` [S12] still works on 4.4.7.1.

**Video.** AVFoundation delivered 3840x2160 frames, and 1920x1080 at 59–60 fps.

**Standard UVC control ranges** (GET_MIN, GET_MAX, GET_DEF):

| Control | Min | Max | Default |
|---|---|---|---|
| exposure time absolute (100 µs) | 1 | 2500 | 330 |
| focus absolute | 0 | 100 | 0 |
| zoom absolute | 0 | 100 | 0 |
| pan, tilt absolute | -648000 | 648000 | 0 |
| backlight compensation | 0 | 18 | 9 |
| brightness, contrast, hue, saturation, sharpness | 0 | 100 | 50 |
| gain | 1 | 64 | 1 |
| power line frequency | 0 | 3 | 3 |
| white balance temperature (K) | 2000 | 10000 | 5000 |

Captured frames confirmed each write: manual focus, manual exposure with gain, manual white balance, zoom, and pan at zoom 100. Zoom and pan move gradually; zoom 100 to 0 takes about 3 s. A write sent while zoom or pan is still moving can be dropped, so read the value back and retry.

**Vendor unit.** Selector-6 writes of flip (`0x14`) and face AE (`0x03`) took effect and read back in status bytes 19 and 7. One GET_CUR sent right after a selector-6 SET_CUR timed out; the next read worked.

**OBSBOT Center settings in the status block.** Changing these in the app moved these bytes:

| Setting turned off | Offset | Before | After |
|---|---|---|---|
| Status light | 33 | `03` | `00` |
| Microphone (Disable Microphone on) | 34 | `10` | `00` |
| Gesture auto framing and gesture zoom | 38 | `8d` | `84` |
| Auto sleep (likely; the only other byte that moved) | 30 | `55` | `00` |

Both gesture switches were turned off together, so which of bits 0 and 3 belongs to which switch is not known. With the microphone disabled, macOS no longer lists it as an audio device.

**Persistence.** With OBSBOT Center quit, the camera was unplugged and plugged back in. The status block was byte-identical to the one before, and the UVC controls kept values set during testing (white balance 9000 K, focus 100, gain 40). The camera stores both its vendor settings and its UVC control values. `uvcvideo` does not write controls on probe [S21], so the box should see the same values, but that is untested on Linux.

**Controlling the camera from macOS.** macOS has no V4L2. libusb control transfers to the VideoControl interface (camera terminal 1, processing unit 3, extension unit 2) work without claiming the interface, which macOS's UVC driver owns. pyusb's `Device.ctrl_transfer` tries to claim it and fails with "Access denied", so call the libusb backend's `ctrl_transfer` with the open device handle instead.

---

## Sources

Retrieved 2026-11-07 unless noted. Git sources were read at the commit given.

- [S1] OBSBOT Meet 2 tech specs: https://obsbot.com/obsbot-meet-2-4k-webcam/specs
- [S2] OBSBOT Meet 2 downloads (OBSBOT Center 2.0.15.186, firmware 4.4.7.1): https://www.obsbot.com/download/obsbot-meet-2
- [S3] OBSBOT Meet 2 User Manual, English section of the Alza-distributed copy: https://dwn.alza.cz/manual/149372
- [S4] OBSBOT Meet 2 FAQ: https://www.obsbot.com/obsbot-meet-2-4k-webcam/faq
- [S5] OBSBOT SDK page: https://www.obsbot.com/sdk
- [S6] OBSBOT news, "November update" (2024-11-05, Meet 2 gesture control): https://obsbot.com/news/november-update
- [S7] OBSBOT Meet 2 product page: https://www.obsbot.com/obsbot-meet-2-4k-webcam
- [S8] linux-hardware.org, USB 3564:fefb: https://linux-hardware.org/?id=usb:3564-fefb
- [S9] aaronsb/obsbot-camera-control README (commit b83c6b8): https://github.com/aaronsb/obsbot-camera-control
- [S10] OBSBOT SDK header `sdk/v1.0.2/include/dev/dev.hpp` in obsbot-camera-control (commit b83c6b8): https://github.com/aaronsb/obsbot-camera-control/blob/main/sdk/v1.0.2/include/dev/dev.hpp
- [S11] obsbot-camera-control `docs/SDK.md` and issue #39 (open UVC protocol): https://github.com/aaronsb/obsbot-camera-control/blob/main/docs/SDK.md, https://github.com/aaronsb/obsbot-camera-control/issues/39
- [S12] strips fork, `docs/MEET2-USB-PROTOCOL.md` (Meet 2, firmware 4.4.6.1; branch docs/meet2-usb-protocol-capture, commit a5beb90): https://github.com/strips/obsbot-camera-control/blob/docs/meet2-usb-protocol-capture/docs/MEET2-USB-PROTOCOL.md
- [S13] strips fork, `docs/MEET2-USB-CATALOG.md`: https://github.com/strips/obsbot-camera-control/blob/docs/meet2-usb-protocol-capture/docs/MEET2-USB-CATALOG.md
- [S14] strips fork, `tools/xu-sweep/xu_raw.py`: https://github.com/strips/obsbot-camera-control/blob/docs/meet2-usb-protocol-capture/tools/xu-sweep/xu_raw.py
- [S15] obsbot-camera-control `src/cli/meet2_test.cpp` (obsbot-cli): https://github.com/aaronsb/obsbot-camera-control/blob/main/src/cli/meet2_test.cpp
- [S16] obsbot-camera-control issue #33 (Meet 2 continuous AF): https://github.com/aaronsb/obsbot-camera-control/issues/33
- [S17] obsbot-camera-control issue #56 (Meet 2 snapshot/v4l2loopback crop): https://github.com/aaronsb/obsbot-camera-control/issues/56
- [S18] soyersoyer/cameractrls README (commit 6f38825): https://github.com/soyersoyer/cameractrls
- [S19] laktosterror/obsbot-camera-control fork README and `src/cli/xu_ctl.cpp` (Meet SE, commit ca12874): https://github.com/laktosterror/obsbot-camera-control
- [S20] Linux `drivers/media/usb/uvc/uvc_driver.c` (master, 7.4-rc1): https://github.com/torvalds/linux/blob/master/drivers/media/usb/uvc/uvc_driver.c
- [S21] Linux `drivers/media/usb/uvc/uvc_ctrl.c` (control table, `uvc_ctrl_restore_values`): https://github.com/torvalds/linux/blob/master/drivers/media/usb/uvc/uvc_ctrl.c
- [S22] V4L2 camera control reference: https://docs.kernel.org/userspace-api/media/v4l/ext-ctrls-camera.html
- [S23] Linux uvcvideo driver documentation (extension unit access, `UVCIOC_CTRL_QUERY`): https://docs.kernel.org/userspace-api/media/drivers/uvcvideo.html
- [S24] systemd.device(5), `SYSTEMD_WANTS` udev property: https://www.freedesktop.org/software/systemd/man/latest/systemd.device.html
- [S25] OBSBOT webcam firmware upgrade manual: https://www.obsbot.com/service/faq/meet-upgrade-manual
- [S26] v4l-utils source, `utils/v4l2-ctl/v4l2-ctl-vidcap.cpp` and `v4l2-ctl-streaming.cpp` (option help strings): https://github.com/gjasny/v4l-utils/tree/master/utils/v4l2-ctl
- [S27] linux-usb.org USB ID list: http://www.linux-usb.org/usb.ids
- [S28] obsbot-camera-control `docs/ROADMAP.md`: https://github.com/aaronsb/obsbot-camera-control/blob/main/docs/ROADMAP.md
- [S29] obsbot-camera-control `src/gui/CameraController.cpp`: https://github.com/aaronsb/obsbot-camera-control/blob/main/src/gui/CameraController.cpp
- [S30] go2rtc `internal/v4l2/README.md` (commit 990f4de): https://github.com/AlexxIT/go2rtc/blob/master/internal/v4l2/README.md
- [S31] go2rtc README, "Codecs madness" table: https://github.com/AlexxIT/go2rtc#codecs-madness
- [S32] go2rtc `internal/ffmpeg/hardware/README.md`: https://github.com/AlexxIT/go2rtc/blob/master/internal/ffmpeg/hardware/README.md
- [S33] go2rtc `pkg/v4l2/producer.go` (`input_format` values yuyv422, mjpeg, h264): https://github.com/AlexxIT/go2rtc/blob/master/pkg/v4l2/producer.go
- [S34] go2rtc `internal/mjpeg/README.md`: https://github.com/AlexxIT/go2rtc/blob/master/internal/mjpeg/README.md
- [S35] go2rtc `www/README.md`: https://github.com/AlexxIT/go2rtc/blob/master/www/README.md
- [S36] MediaMTX docs, "Generic webcams" (commit ef25331): https://github.com/bluenviron/mediamtx/blob/main/docs/3-publish/06-generic-webcams.md
- [S37] FFmpeg devices documentation, video4linux2 input (`doc/indevs.texi`): https://ffmpeg.org/ffmpeg-devices.html#video4linux2_002c-v4l2
- [S38] FFmpeg documentation, `-init_hw_device vaapi` (`doc/ffmpeg.texi`): https://ffmpeg.org/ffmpeg.html#Advanced-Video-options
- [S39] FFmpeg codecs/filters documentation, VAAPI encoders and `hwupload`: https://ffmpeg.org/ffmpeg-codecs.html#VAAPI-encoders, https://ffmpeg.org/ffmpeg-filters.html#hwupload
- [S40] MediaMTX docs, "Publish and read" (protocols and ports): https://github.com/bluenviron/mediamtx/blob/main/docs/2-features/03-publish-and-read.md
- [S41] GStreamer `v4l2src` documentation: https://gstreamer.freedesktop.org/documentation/video4linux2/v4l2src.html
- [S42] OpenFoxes/Tiny4Linux README (Tiny 2; Windows VM workaround): https://github.com/OpenFoxes/Tiny4Linux
- [S43] obsbot-camera-control `src/gui/CameraPreviewWidget.cpp` (`captureSnapshot`): https://github.com/aaronsb/obsbot-camera-control/blob/main/src/gui/CameraPreviewWidget.cpp
- [S44] obs-studio `plugins/linux-v4l2/v4l2-input.c`: https://github.com/obsproject/obs-studio/blob/master/plugins/linux-v4l2/v4l2-input.c
- [S45] obs-studio `plugins/linux-v4l2/v4l2-decoder.c` and `v4l2-helpers.h`: https://github.com/obsproject/obs-studio/tree/master/plugins/linux-v4l2
- [S46] OBS forum, "Controling v4l2 device controls from sources" and "Video Capture Device (v4l2) plugin" threads (anecdotal): https://obsproject.com/forum/threads/controling-v4l2-device-controls-from-sources.62823/, https://obsproject.com/forum/threads/video-capture-device-v4l2-plugin.17358/
- [S47] obs-studio `plugins/linux-v4l2/v4l2-controls.c`: https://github.com/obsproject/obs-studio/blob/master/plugins/linux-v4l2/v4l2-controls.c
- [S48] OBSBOT Meet 2 "Explore" page (Stream Deck plugin, OSC, upgrade manual): https://www.obsbot.com/explore/obsbot-meet-2
- [S49] glikely/obs-ptz: https://github.com/glikely/obs-ptz
- [S50] obsbot-camera-control issue list (#57 Wayland v4l2loopback freeze): https://github.com/aaronsb/obsbot-camera-control/issues?q=is%3Aissue
- [S51] V4L2 user controls (`V4L2_CID_POWER_LINE_FREQUENCY`, 1 = 50 Hz): https://docs.kernel.org/userspace-api/media/v4l/control.html
