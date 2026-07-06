#!/usr/bin/env node
/**
 * One-time script to generate the Gmail OAuth2 token.
 * Run once locally, then copy the generated token.json to the server.
 *
 * Usage:
 *   GMAIL_CREDENTIALS_PATH=./credentials.json node setup-gmail-auth.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { google } = require('googleapis');

const SCOPES = ['https://www.googleapis.com/auth/gmail.readonly'];
const credPath = process.env.GMAIL_CREDENTIALS_PATH ?? './credentials.json';
const tokPath = process.env.GMAIL_TOKEN_PATH ?? './token.json';

async function main() {
  const credentials = JSON.parse(fs.readFileSync(credPath, 'utf8'));
  const { client_secret, client_id, redirect_uris } = credentials.installed ?? credentials.web;
  const oAuth2 = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);

  const authUrl = oAuth2.generateAuthUrl({ access_type: 'offline', scope: SCOPES });
  console.log('1. Open this URL in your browser:\n');
  console.log('  ', authUrl, '\n');

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  rl.question('2. Paste the authorisation code here: ', async code => {
    rl.close();
    const { tokens } = await oAuth2.getToken(code.trim());
    fs.writeFileSync(tokPath, JSON.stringify(tokens, null, 2));
    console.log(`\nToken saved to ${path.resolve(tokPath)}`);
  });
}

main().catch(console.error);
