      * Test driver: runs one GenApp program as if CICS had started it
      * with a COMMAREA. Program name and COMMAREA come from environment
      * variables (see genapprt.c CICSINIT); the updated COMMAREA is
      * written back when the program returns.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CICSRUN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  RUN-PROGRAM               PIC X(8).
       01  RUN-CALEN                 PIC S9(9) COMP-5.
       01  RUN-ABCODE                PIC X(4) VALUE 'AEI0'.
       01  DFHEIBLK.
           COPY CICSEIB.
       01  RUN-COMMAREA              PIC X(32767).
       PROCEDURE DIVISION.
           CALL STATIC 'CICSINIT' USING RUN-PROGRAM RUN-COMMAREA
                                        RUN-CALEN
           INITIALIZE DFHEIBLK
           MOVE RUN-CALEN TO EIBCALEN
           MOVE 'GNAP' TO EIBTRNID
           MOVE 'TEST' TO EIBTRMID
           MOVE 1 TO EIBTASKN
           CALL RUN-PROGRAM USING DFHEIBLK RUN-COMMAREA
             ON EXCEPTION
               DISPLAY 'PGMIDERR: ' RUN-PROGRAM UPON SYSERR
               CALL STATIC 'CICSABND' USING RUN-ABCODE END-CALL
           END-CALL
           CALL STATIC 'CICSTERM' USING RUN-COMMAREA RUN-CALEN
           STOP RUN.
