{
  description = "Pomodorough Linux desktop, command-line, and terminal clients";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          wasmtimeWheel =
            if system == "x86_64-linux" then {
              url = "https://files.pythonhosted.org/packages/d5/9d/97c109d4a25c716c4045644398d7e684e1d4fa5ec6c99412b5cba35a9a98/wasmtime-50.0.1-py3-none-manylinux1_x86_64.whl";
              hash = "sha256-zBTDoXZU4DlOAiWxGBNTLvDNe9HiIqDFSUze2qvt2sQ=";
              name = "wasmtime-50.0.1-py3-none-manylinux1_x86_64.whl";
            } else {
              url = "https://files.pythonhosted.org/packages/8e/c4/58709b73e613e5366e4783537e2a861272d95f500c43fddf9b378d98415a/wasmtime-50.0.1-py3-none-manylinux2014_aarch64.whl";
              hash = "sha256-Z1SE5SXAKNwCdit3fx20R36nDKo/7Gd1Qm2v9NZO37E=";
              name = "wasmtime-50.0.1-py3-none-manylinux2014_aarch64.whl";
            };
          wasmtime = pkgs.python3.pkgs.buildPythonPackage {
            pname = "wasmtime";
            version = "50.0.1";
            format = "wheel";
            src = pkgs.fetchurl wasmtimeWheel;
            doCheck = false;
            nativeBuildInputs = [ pkgs.autoPatchelfHook ];
            pythonImportsCheck = [ "wasmtime" ];
          };
        in
        {
          default = pkgs.python3.pkgs.buildPythonApplication {
            pname = "pomodorough-linux";
            version = "0.56.0";
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./LICENSE
                ./README.md
                ./pyproject.toml
                ./src
                ./deploy
              ];
            };

            build-system = [ pkgs.python3.pkgs.setuptools ];
            # iroh is binary-wheel-only and unavailable in nixpkgs on every
            # supported system. Nix build remains fully offline-capable and
            # reports Iroh as unavailable instead of attempting PyPI resolution.
            dependencies = [
              pkgs.python3.pkgs.platformdirs
              pkgs.python3.pkgs.pyside6
              pkgs.python3.pkgs.wcwidth
              wasmtime
            ];
            pythonRemoveDeps = [ "PySide6-Essentials" ];
            nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
            buildInputs = [ pkgs.qt6.qtwayland ];
            # The provider belongs to the host desktop session, not this package.
            makeWrapperArgs = [ "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.libsecret ]}" ];

            postInstall = ''
              install -Dm644 src/pomodorough/resources/icon.svg \
                $out/share/icons/hicolor/scalable/apps/me.egigoka.Pomodorough.svg
              install -Dm644 deploy/me.egigoka.Pomodorough.desktop \
                $out/share/applications/me.egigoka.Pomodorough.desktop
              substituteInPlace $out/share/applications/me.egigoka.Pomodorough.desktop \
                --replace-fail "/usr/bin/env -u LOCALE_ARCHIVE_2_27 @EXEC@" "$out/bin/pomodorough" \
                --replace-fail "@ICON@" "me.egigoka.Pomodorough"
              install -Dm644 deploy/flatpak/me.egigoka.Pomodorough.metainfo.xml \
                $out/share/metainfo/me.egigoka.Pomodorough.metainfo.xml
            '';

            pythonImportsCheck = [ "pomodorough" "pomodorough.shared_core" "pomodorough.tui" ];
            doInstallCheck = true;
            installCheckPhase = ''
              runHook preInstallCheck
              PYTHONPATH="$out/${pkgs.python3.sitePackages}:$PYTHONPATH" \
                ${pkgs.python3.interpreter} -c \
                'from pomodorough.shared_core import SharedCore; assert SharedCore().dispatch("core.version", {})["schemaVersion"] == 1'
              PYTHONPATH="$out/${pkgs.python3.sitePackages}:$PYTHONPATH" \
                ${pkgs.python3.interpreter} -c \
                'from pomodorough import tui'
              runHook postInstallCheck
            '';

            meta = {
              description = "KDE-first, local-first Pomodoro timer";
              homepage = "https://github.com/Pomodoro-Everywhere/pomodorough-desktop";
              license = pkgs.lib.licenses.gpl3Plus;
              mainProgram = "pomodorough";
              platforms = pkgs.lib.platforms.linux;
            };
          };
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/pomodorough";
          meta.description = "Run the Pomodorough desktop timer";
        };
      });
    };
}
