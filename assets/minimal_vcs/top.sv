module top;
  reg clk = 0;
  reg valid = 0;
  reg [2:0] data = 0;

  initial begin
    $fsdbDumpfile("waves.fsdb");
    $fsdbDumpvars(0, top);
    repeat (8) begin
      #5 clk = ~clk;
      valid = (data[0] == 1'b0);
      data = data + 1'b1;
    end
    $finish;
  end
endmodule
