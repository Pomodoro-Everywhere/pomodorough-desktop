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
              url = "https://files.pythonhosted.org/packages/a2/92/e144fcf578fc394678c24b042efe45f3b0614acdb87ea95d8b839b208842/wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl";
              hash = "sha256-WFRNU5BT3/e9TPMMQNelQIYtaDATwN+mukagY/W2gvc=";
              name = "wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl";
            } else {
              url = "https://files.pythonhosted.org/packages/1c/c3/a957b226979daaeb09ec024562e9aac05e475a954537e6f150eb60bca84d/wasmtime-48.0.0-py3-none-manylinux2014_aarch64.whl";
              hash = "sha256-JvzjYT/vvimijp1lncozJugAWThY5XWMrQhuuAKzt2Y=";
              name = "wasmtime-48.0.0-py3-none-manylinux2014_aarch64.whl";
            };
          wasmtime = pkgs.python3.pkgs.buildPythonPackage {
            pname = "wasmtime";
            version = "48.0.0";
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
            version = "0.4.2";
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
              wasmtime
            ];
            pythonRemoveDeps = [ "PySide6-Essentials" ];
            nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
            buildInputs = [ pkgs.qt6.qtwayland ];

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

            pythonImportsCheck = [ "pomodorough" "pomodorough.shared_core" ];
            doInstallCheck = true;
            installCheckPhase = ''
              runHook preInstallCheck
              PYTHONPATH="$out/${pkgs.python3.sitePackages}:$PYTHONPATH" \
                ${pkgs.python3.interpreter} -c \
                'from pomodorough.shared_core import SharedCore; assert SharedCore().dispatch("core.version", {})["schemaVersion"] == 1'
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
