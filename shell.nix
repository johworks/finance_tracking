{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  
  packages = [
    (pkgs.python3.withPackages(pypkgs: with pypkgs; [
      pandas
      sqlalchemy
      flask
    ]))
  ];
}
