      * EXEC interface block fields used by GenApp (test runtime).
      * EIBXINVK is an extension: name of the program that issued the
      * LINK, used for EXEC CICS ASSIGN INVOKINGPROG.
           02 EIBTIME                PIC S9(7) COMP-3.
           02 EIBDATE                PIC S9(7) COMP-3.
           02 EIBTRNID               PIC X(4).
           02 EIBTASKN               PIC S9(7) COMP-3.
           02 EIBTRMID               PIC X(4).
           02 EIBCPOSN               PIC S9(4) COMP-5.
           02 EIBCALEN               PIC S9(4) COMP-5.
           02 EIBAID                 PIC X.
           02 EIBFN                  PIC X(2).
           02 EIBRCODE               PIC X(6).
           02 EIBDS                  PIC X(8).
           02 EIBREQID               PIC X(8).
           02 EIBRSRCE               PIC X(8).
           02 EIBRESP                PIC S9(8) COMP-5.
           02 EIBRESP2               PIC S9(8) COMP-5.
           02 EIBXINVK               PIC X(8).
